"""Run production action/webhook handlers with real transport-layer managers.

HA imports require its complete runtime. Compile the production functions with
postponed annotations and supply only framework boundaries; their bodies and
all routing, persistence, observation and provider send code remain unchanged.
"""

import ast
import asyncio
import hashlib
import hmac
import json
import logging
import re
import sys
import time
from collections.abc import Mapping
from copy import deepcopy
from functools import partial
from http import HTTPStatus
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import cast
from unittest.mock import AsyncMock

import pytest
from aiohttp.web import Response

from ._loader import INTEGRATION_PATH, load_integration_module

api = load_integration_module("api")
const = load_integration_module("const")
registry_module = load_integration_module("channel_registry")
correlated = load_integration_module("correlated")
outbound = load_integration_module("outbound")
groups = load_integration_module("general_groups")
polls = load_integration_module("polls")


class FrameworkError(Exception):
    """Keep the actual handler's translation metadata inspectable."""

    def __init__(self, **kwargs):
        self.translation_key = kwargs.get("translation_key")
        super().__init__(self.translation_key)


class Store:
    """Snapshot persistence; provider I/O is the only mocked send boundary."""

    def __init__(self):
        self.data = None
        self.fail = False

    async def async_load(self):
        return deepcopy(self.data)

    async def async_save(self, data):
        if self.fail:
            raise OSError("private storage failure")
        self.data = deepcopy(data)


def _functions(path, names, namespace, *, methods=False):
    tree = ast.parse(Path(path).read_text())
    candidates = ast.walk(tree) if methods else tree.body
    selected = []
    for node in candidates:
        if (
            isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
            and node.name in names
        ):
            node.decorator_list = []
            selected.append(node)
    assert {node.name for node in selected} == set(names)
    module = ast.Module(
        body=ast.parse("from __future__ import annotations").body + selected,
        type_ignores=[],
    )
    exec(compile(ast.fix_missing_locations(module), str(path), "exec"), namespace)
    return namespace


def _handlers():
    namespace = {key: value for key, value in vars(const).items() if key.isupper()}
    namespace.update(
        ConfigEntryState=SimpleNamespace(LOADED="loaded"),
        CONF_CONFIG_ENTRY_ID="config_entry_id",
        ServiceValidationError=FrameworkError,
        HomeAssistantError=FrameworkError,
        WahaConfigEntry=SimpleNamespace,
        WahaError=api.WahaError,
        CorrelatedSendError=correlated.CorrelatedSendError,
        GeneralGroupError=groups.GeneralGroupError,
        async_send_observed=outbound.async_send_observed,
        _LOGGER=logging.getLogger(__name__),
        MIN_GOWS_VERSION=(2026, 8, 2),
        MAX_WEBHOOK_BODY_BYTES=256 * 1024,
        PLATFORMS=["notify"],
        HTTPStatus=HTTPStatus,
        Response=Response,
        cast=cast,
        re=re,
    )
    return _functions(
        INTEGRATION_PATH / "__init__.py",
        {
            "_async_handle_send_to_conversation",
            "_contact_content_redacted",
            "_async_handle_get_channel_capabilities",
            "_loaded_entry",
            "_supports_guest_webhooks",
            "_async_handle_waha_webhook",
            "async_unload_entry",
        },
        namespace,
    )


async def _setup(monkeypatch, *, group=False, observations=True):
    monkeypatch.setattr(
        registry_module, "_notify_entity_id", lambda *_args: "notify.contact"
    )
    bus = SimpleNamespace(events=[])
    bus.async_fire = lambda event, payload: bus.events.append(
        (event, deepcopy(payload))
    )
    hass = SimpleNamespace(bus=bus)
    direct = SimpleNamespace(
        subentry_id="contact",
        subentry_type="recipient",
        data={"recipient": "393331234567"},
    )
    general = SimpleNamespace(
        subentry_id="group",
        subentry_type="group",
        data={
            "group_id": "123456789@g.us",
            "enabled": True,
            "bot_account": "393339999999@c.us",
        },
    )
    entry = SimpleNamespace(
        entry_id="entry",
        domain=const.DOMAIN,
        state="loaded",
        title="Channel",
        data={"webhook_secret": "private-signing-secret", "session": "default"},
        subentries={"contact": direct, **({"group": general} if group else {})},
        options={"outbound_observations_enabled": observations},
    )
    client = api.WahaClient(
        None, "private-api-key", "default", base_url="http://invalid"
    )
    client._request = AsyncMock(return_value={"id": "provider-message"})
    server = api.WahaServer("2026.8.2", "GOWS", "PLUS")
    client.async_get_server = AsyncMock(return_value=server)
    client.async_get_session = AsyncMock(
        return_value=api.WahaSession("default", "WORKING", "393339999999@c.us", None)
    )
    client.async_get_group = AsyncMock(return_value={"id": general.data["group_id"]})
    client.async_get_group_participants = AsyncMock(
        return_value=[
            api.WahaGroupParticipant("393339999999@c.us", "participant", None),
            api.WahaGroupParticipant("393331234567@c.us", "participant", None),
        ]
    )
    route_store = Store()
    registry = registry_module.ChannelRegistry(hass, entry, client, store=route_store)
    await registry.async_start()
    guest = SimpleNamespace(
        saved_group_jid=None,
        resolve_group_conversation=lambda _route: None,
        resolve_route=lambda _route: None,
    )
    general_groups = groups.GeneralGroupManager(hass, entry, client, registry, guest)
    registry.set_extra_route_resolver(general_groups.conversation_destination)
    dedup = correlated.CorrelatedSendManager(hass, entry, store=Store())
    await dedup.async_start()
    observer = outbound.OutboundObserver(hass, entry, registry)
    entry.runtime_data = SimpleNamespace(
        channel_registry=registry,
        correlated_sends=dedup,
        general_groups=general_groups,
        guest_registry=guest,
        client=client,
        server=server,
        outbound_observer=observer,
    )
    hass.config_entries = SimpleNamespace(
        async_entries=lambda _domain: [entry],
        async_get_entry=lambda entry_id: entry if entry_id == entry.entry_id else None,
    )
    direct_route = registry.resolve_conversation(
        registry._conversation_id("contact", direct.data["recipient"])
    ).conversation_id
    group_route = (
        general_groups.resolve_subentry("group").conversation_id if group else None
    )
    return SimpleNamespace(
        entry=entry,
        hass=hass,
        registry=registry,
        store=route_store,
        client=client,
        dedup=dedup,
        groups=general_groups,
        direct=direct_route,
        group=group_route,
        handlers=_handlers(),
    )


def _call(env, *, route=None, key=None, quote=None, response=True):
    data = {"conversation_id": route or env.direct, "message": "Rendered reply"}
    if key is not None:
        data["client_request_id"] = key
    if quote is not None:
        data["reply_to_message_id"] = quote
    return SimpleNamespace(hass=env.hass, data=data, return_response=response)


async def _send(env, **kwargs):
    return await env.handlers["_async_handle_send_to_conversation"](
        _call(env, **kwargs)
    )


@pytest.mark.asyncio
async def test_unload_drains_admitted_send_before_replacement(monkeypatch):
    """Reload cannot replace a runtime while its keyed outcome can still write."""
    env = await _setup(monkeypatch)
    entered = asyncio.Event()
    release = asyncio.Event()
    platforms_unloaded = asyncio.Event()

    async def provider(*_args, **_kwargs):
        entered.set()
        await release.wait()
        return {"id": "accepted-before-unload"}

    async def unload(_entry, _platforms):
        platforms_unloaded.set()
        return True

    env.client._request.side_effect = provider
    env.hass.config_entries.async_unload_platforms = unload
    send = asyncio.create_task(_send(env, key="inflight"))
    await entered.wait()
    unloading = asyncio.create_task(
        env.handlers["async_unload_entry"](env.hass, env.entry)
    )
    await platforms_unloaded.wait()
    await asyncio.sleep(0)
    assert not unloading.done()
    blocked = await _send(env, key="after-unload-begins")
    assert blocked["status"] == "failed"
    assert blocked["error_code"] == "channel_unavailable"
    release.set()
    accepted = await send
    assert accepted["status"] == "accepted"
    assert await unloading is True
    assert (await _send(env, key="inflight")) == {**accepted, "replayed": True}
    env.client._request.assert_awaited_once()


@pytest.mark.asyncio
async def test_failed_platform_unload_preserves_keyed_admission(monkeypatch):
    """An unsuccessful HA unload leaves the current communication path usable."""
    env = await _setup(monkeypatch)
    env.hass.config_entries.async_unload_platforms = AsyncMock(return_value=False)
    assert await env.handlers["async_unload_entry"](env.hass, env.entry) is False
    result = await _send(env, key="still-available")
    assert result["status"] == "accepted"
    env.client._request.assert_awaited_once()


@pytest.mark.asyncio
async def test_legacy_response_and_no_response_contract_is_unchanged(monkeypatch):
    env = await _setup(monkeypatch)
    token = env.registry.message_token("provider-message")
    assert await _send(env) == {"conversation_id": env.direct, "message_id": token}
    assert await _send(env, response=False) is None
    assert env.client._request.await_count == 2
    assert len(env.hass.bus.events) == 2
    assert all(event[1]["status"] == "accepted" for event in env.hass.bus.events)


@pytest.mark.asyncio
async def test_keyed_replay_survives_quote_expiry_and_conflict_has_no_io(monkeypatch):
    env = await _setup(monkeypatch)
    quote = await env.registry.async_remember_message("incoming", env.direct)
    first = await _send(env, key="request", quote=quote)
    assert first["status"] == "accepted"
    # Expire only quote storage. Durable request remains inside its own window.
    env.registry._messages[quote].expires_at = 0
    duplicate = await _send(env, key="request", quote=quote)
    assert duplicate == {**first, "replayed": True}
    call = _call(env, key="request", quote=quote)
    call.data["message"] = "Conflicting content"
    conflict = await env.handlers["_async_handle_send_to_conversation"](call)
    assert conflict["error_code"] == "request_conflict"
    env.client._request.assert_awaited_once()
    assert len(env.hass.bus.events) == 1
    sent = env.client._request.call_args.kwargs["json"]
    assert sent["reply_to"] == "incoming"
    assert sent["chatId"] == "393331234567@c.us"


@pytest.mark.asyncio
@pytest.mark.parametrize("quote_state", ["invalid", "expired", "cross_conversation"])
async def test_invalid_quotes_return_safe_error_without_network(
    monkeypatch, quote_state
):
    env = await _setup(monkeypatch, group=True)
    quote = "msg_unknown"
    if quote_state == "expired":
        quote = await env.registry.async_remember_message("incoming", env.direct)
        env.registry._messages[quote].expires_at = 0
    elif quote_state == "cross_conversation":
        quote = await env.registry.async_remember_message("group-incoming", env.group)
    result = await _send(env, key="request", quote=quote)
    assert result["status"] == "failed"
    assert result["error_code"] == "invalid_reply_target"
    env.client._request.assert_not_awaited()
    assert env.hass.bus.events == []
    with pytest.raises(FrameworkError, match="invalid_reply_target"):
        await _send(env, quote=quote)


@pytest.mark.asyncio
async def test_accepted_tracking_failure_never_resends_and_degrades_health(monkeypatch):
    env = await _setup(monkeypatch)
    env.store.fail = True
    result = await _send(env, key="request")
    assert result["status"] == "accepted"
    assert result.get("message_id") is None
    assert (await _send(env, key="request"))["replayed"]
    env.client._request.assert_awaited_once()
    observation = env.hass.bus.events[0][1]
    assert observation["status"] == "accepted"
    assert observation["message"]["id"] is None
    assert env.entry.runtime_data.outbound_observer.health == "degraded"


@pytest.mark.asyncio
async def test_general_group_reply_uses_real_provider_signature_and_quote(monkeypatch):
    env = await _setup(monkeypatch, group=True)
    quote = await env.registry.async_remember_message("group-incoming", env.group)
    result = await _send(env, route=env.group, key="request", quote=quote)
    assert result["status"] == "accepted"
    env.client._request.assert_awaited_once()
    payload = env.client._request.call_args.kwargs["json"]
    assert payload["chatId"] == "123456789@g.us"
    assert payload["reply_to"] == "group-incoming"
    assert env.client.async_get_group_participants.await_count == 1
    observation = env.hass.bus.events[0][1]
    assert observation["conversation_type"] == "group"
    assert observation["message"]["in_reply_to"] == quote


@pytest.mark.asyncio
async def test_disabled_group_route_fails_without_fallback(monkeypatch):
    env = await _setup(monkeypatch, group=True)
    env.entry.subentries["group"].data["enabled"] = False
    result = await _send(env, route=env.group, key="request")
    assert result["error_code"] == "invalid_conversation"
    env.client._request.assert_not_awaited()
    with pytest.raises(FrameworkError, match="invalid_conversation"):
        await _send(env, route=env.group)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("connected", "persistent", "enabled"),
    [
        (True, True, True),
        (False, True, True),
        (True, False, True),
        (True, True, False),
    ],
)
async def test_capabilities_reflect_connection_persistence_and_opt_in(
    monkeypatch, connected, persistent, enabled
):
    env = await _setup(monkeypatch, group=True, observations=enabled)
    env.dedup.persistence_available = persistent
    if not connected:
        env.client.async_get_session.side_effect = api.WahaConnectionError(
            "private detail"
        )
    call = SimpleNamespace(hass=env.hass, data={"config_entry_id": "entry"})
    result = await env.handlers["_async_handle_get_channel_capabilities"](call)
    assert result["correlated_sends"] is (connected and persistent)
    assert result["outbound_observations"] is (connected and enabled)
    assert result["general_groups"] is connected
    assert result["mentions"] is connected
    assert result["delivery_receipts"] is False
    assert result["media_retrieval"] is False
    assert result["transport_replay"] is False
    assert result["idempotency_retention_seconds"] == 604800
    if not connected:
        assert result["observation_health"] == "unavailable"
    assert "private detail" not in str(result)
    assert "39333" not in str(result)


def _group_inbound_module():
    stub = ModuleType("homeassistant.core")
    stub.Context = SimpleNamespace
    previous = sys.modules.get("homeassistant.core")
    sys.modules["homeassistant.core"] = stub
    try:
        return load_integration_module("general_group_inbound")
    finally:
        if previous is None:
            del sys.modules["homeassistant.core"]
        else:
            sys.modules["homeassistant.core"] = previous


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("signature_ok", "session", "expected"),
    [
        (True, "default", 1),
        (False, "default", 0),
        (True, "other", 0),
    ],
)
async def test_signed_dispatch_keeps_group_session_validation(
    monkeypatch, signature_ok, session, expected
):
    env = await _setup(monkeypatch, group=True)
    inbound_module = _group_inbound_module()
    inbound = inbound_module.WahaGeneralGroupInboundManager(
        env.hass, env.entry, env.groups, env.registry
    )
    parser = _functions(
        INTEGRATION_PATH / "poll_manager.py",
        {"verify_signature", "decode_payload"},
        {
            "verify_hmac_sha512": polls.verify_hmac_sha512,
            "json": json,
            "Mapping": Mapping,
        },
        methods=True,
    )
    poll = SimpleNamespace(
        verify_signature=partial(
            parser["verify_signature"],
            SimpleNamespace(_webhook_secret="private-signing-secret"),
        ),
        decode_payload=parser["decode_payload"],
        async_handle_payload=AsyncMock(),
    )
    envelope = {
        "id": "event-group",
        "event": "message",
        "session": session,
        "payload": {
            "id": "group-message",
            "timestamp": time.time(),
            "from": "123456789@g.us",
            "fromMe": False,
            "participant": "393331234567@c.us",
            "body": "Hello",
            "hasMedia": False,
            "_data": {
                "Message": {
                    "extendedTextMessage": {
                        "contextInfo": {"mentionedJID": ["393339999999@c.us"]}
                    }
                }
            },
        },
    }
    body = json.dumps(envelope).encode()
    signature = hmac.new(b"private-signing-secret", body, hashlib.sha512).hexdigest()
    request = SimpleNamespace(
        headers={
            "X-Webhook-Hmac-Algorithm": "sha512",
            "X-Webhook-Hmac": signature if signature_ok else "forged",
        },
        content=SimpleNamespace(read=AsyncMock(return_value=body)),
    )
    response = await env.handlers["_async_handle_waha_webhook"](
        poll,
        SimpleNamespace(async_handle_payload=AsyncMock()),
        SimpleNamespace(async_handle_webhook=AsyncMock()),
        SimpleNamespace(async_handle_payload=AsyncMock(return_value=False)),
        False,
        env.hass,
        "webhook",
        request,
        general_group_inbound=inbound,
    )
    assert response.status == (200 if signature_ok else 401)
    assert len(env.hass.bus.events) == expected
    if expected:
        event = env.hass.bus.events[0][1]
        assert event["conversation_id"] == env.group
        assert event["sender"]["identity_status"] == "verified"
        assert event["sender"]["participant_id"].startswith("participant_")
        assert event["message"]["mentions"]["bot"] is True
        assert "393331234567" not in str(event)


@pytest.mark.asyncio
async def test_concurrent_service_duplicates_publish_only_one_attempt(monkeypatch):
    env = await _setup(monkeypatch)
    entered = asyncio.Event()
    release = asyncio.Event()

    async def provider(*_args, **_kwargs):
        entered.set()
        await release.wait()
        return {"id": "accepted-after-wait"}

    env.client._request.side_effect = provider
    first = asyncio.create_task(_send(env, key="request"))
    await entered.wait()
    duplicate = await _send(env, key="request")
    assert duplicate["status"] == "pending"
    assert duplicate["replayed"]
    assert env.hass.bus.events == []
    release.set()
    assert (await first)["status"] == "accepted"
    env.client._request.assert_awaited_once()
    assert len(env.hass.bus.events) == 1


@pytest.mark.asyncio
async def test_general_group_disabled_during_verification_never_submits(monkeypatch):
    env = await _setup(monkeypatch, group=True)

    async def roster(_group):
        env.entry.subentries["group"].data["enabled"] = False
        return [api.WahaGroupParticipant("393339999999@c.us", "participant", None)]

    env.client.async_get_group_participants.side_effect = roster
    result = await _send(env, route=env.group, key="request")
    assert result["status"] == "failed"
    assert result["error_code"] == "invalid_conversation"
    env.client._request.assert_not_awaited()


@pytest.mark.asyncio
async def test_unsupported_backend_never_advertises_groups(monkeypatch):
    env = await _setup(monkeypatch, group=True)
    env.client.async_get_server.return_value = api.WahaServer(
        "2026.7.1", "GOWS", "PLUS"
    )
    call = SimpleNamespace(hass=env.hass, data={"config_entry_id": "entry"})
    result = await env.handlers["_async_handle_get_channel_capabilities"](call)
    assert result["general_groups"] is False
    assert result["mentions"] is False
    assert result["correlated_sends"] is True
    env.client.async_get_group_participants.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("route_state", ["disabled", "removed"])
@pytest.mark.parametrize("outcome", ["accepted", "unknown"])
async def test_saved_keyed_outcome_survives_route_removal(
    monkeypatch, route_state, outcome
):
    env = await _setup(monkeypatch, group=True)
    if outcome == "unknown":
        env.client._request.side_effect = api.WahaConnectionError(
            "ambiguous provider outcome"
        )
    first = await _send(env, route=env.group, key="request")
    assert first["status"] == outcome
    if route_state == "disabled":
        env.entry.subentries["group"].data["enabled"] = False
    else:
        del env.entry.subentries["group"]
    replay = await _send(env, route=env.group, key="request")
    assert replay == {**first, "replayed": True}
    fresh = await _send(env, route=env.group, key="new_request")
    assert fresh["status"] == "failed"
    assert fresh["error_code"] == "invalid_conversation"
    env.client._request.assert_awaited_once()
    assert len(env.hass.bus.events) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("provider_body", [None, [], "malformed"])
async def test_malformed_success_body_is_unknown_and_never_resubmits(
    monkeypatch, provider_body
):
    env = await _setup(monkeypatch)
    env.client._request.return_value = provider_body
    first = await _send(env, key="request")
    assert first["status"] == "unknown"
    assert first["message_id"] is None
    assert first["error_code"] == "outcome_unknown"
    assert await _send(env, key="request") == {**first, "replayed": True}
    env.client._request.assert_awaited_once()
    assert env.hass.bus.events[0][1]["status"] == "unknown"
    assert len(env.hass.bus.events) == 1


@pytest.mark.asyncio
async def test_legacy_malformed_success_keeps_existing_response_shape(monkeypatch):
    env = await _setup(monkeypatch)
    env.client._request.return_value = []
    assert await _send(env) == {"conversation_id": env.direct}
    env.client._request.assert_awaited_once()
    assert env.hass.bus.events[0][1]["status"] == "unknown"
