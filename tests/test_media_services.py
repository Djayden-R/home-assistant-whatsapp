"""Exercise actual service handlers and effective discovery with HA boundaries."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from ._loader import INTEGRATION_PATH, load_integration_module
from .test_channel_services import _functions, _setup

media = load_integration_module("media")


async def _services(monkeypatch):
    env = await _setup(monkeypatch)
    env.hass.auth = SimpleNamespace(
        async_get_user=AsyncMock(
            return_value=SimpleNamespace(id="admin", is_active=True, is_admin=True)
        )
    )
    env.manager = SimpleNamespace(
        enabled=True,
        async_get_media=AsyncMock(return_value={"status": "ready", "download": {}}),
        backend_ready=AsyncMock(return_value=True),
        capabilities={
            "version": 1,
            "voice_note_detection": True,
            "voice_note_send": True,
            "inbound_kinds": ["audio", "image", "file"],
            "outbound_kinds": ["audio", "image", "file"],
        },
    )
    env.entry.runtime_data.media_manager = env.manager
    env.handlers["MediaError"] = media.MediaError
    env.handlers["media_observation_binding"] = lambda *_args: "exact-binding"
    _functions(
        INTEGRATION_PATH / "__init__.py",
        {
            "_async_media_principal",
            "_async_handle_get_media",
            "_async_handle_send_media",
        },
        env.handlers,
    )
    env.call = SimpleNamespace(
        hass=env.hass,
        context=SimpleNamespace(user_id="admin"),
        data={
            "config_entry_id": "entry",
            "conversation_id": env.direct,
            "message_id": "msg_example",
            "attachment_id": "att_example",
            "max_bytes": 1000,
        },
    )
    return env


@pytest.mark.parametrize("user", [None, "inactive", "nonadmin", "no_context"])
async def test_get_media_rejects_missing_or_revoked_admin_before_lookup(
    monkeypatch, user
):
    env = await _services(monkeypatch)
    if user == "no_context":
        env.call.context.user_id = None
    elif user is None:
        env.hass.auth.async_get_user.return_value = None
    else:
        env.hass.auth.async_get_user.return_value = SimpleNamespace(
            id="admin", is_active=user != "inactive", is_admin=user != "nonadmin"
        )
    result = await env.handlers["_async_handle_get_media"](env.call)
    assert result == {
        "status": "failed",
        "error": {"code": "not_authorized", "retryable": False},
    }
    env.manager.async_get_media.assert_not_called()


async def test_get_media_forwards_exact_binding_and_tightened_ceiling(monkeypatch):
    env = await _services(monkeypatch)
    assert (await env.handlers["_async_handle_get_media"](env.call))[
        "status"
    ] == "ready"
    env.manager.async_get_media.assert_awaited_once_with(
        env.direct, "msg_example", "att_example", 1000
    )


@pytest.mark.parametrize(
    ("connected", "persistent", "enabled"),
    [
        (True, True, True),
        (False, True, True),
        (True, False, True),
        (True, True, False),
    ],
)
async def test_media_capabilities_are_nested_and_effective(
    monkeypatch, connected, persistent, enabled
):
    env = await _services(monkeypatch)
    env.manager.enabled = enabled
    env.entry.runtime_data.correlated_sends.persistence_available = persistent
    env.client.async_get_session.return_value = SimpleNamespace(
        status="WORKING" if connected else "STOPPED", account_id="123@c.us"
    )
    result = await env.handlers["_async_handle_get_channel_capabilities"](env.call)
    assert "inbound_kinds" not in result
    assert result["media_retrieval"] is (connected and enabled)
    assert result["media_send"] is (connected and enabled and persistent)
    contract = result["media_contract"]
    assert contract["voice_note_detection"] is result["media_retrieval"]
    assert contract["voice_note_send"] is result["media_send"]
    assert bool(contract["outbound_kinds"]) is result["media_send"]
    assert bool(contract["inbound_kinds"]) is result["media_retrieval"]


async def test_send_rechecks_admin_after_slow_preparation(monkeypatch):
    env = await _services(monkeypatch)
    route = AsyncMock(return_value={"status": "accepted"})
    env.handlers["async_media_send_route"] = route

    async def send(_entry, principal, _data, *, async_route):
        assert principal == "admin"
        env.hass.auth.async_get_user.return_value = None
        return await async_route(env.direct, AsyncMock())

    env.handlers["async_send_media"] = send
    result = await env.handlers["_async_handle_send_media"](env.call)
    assert result["error_code"] == "not_authorized"
    route.assert_not_awaited()


async def test_send_rechecks_admin_after_roster_verification(monkeypatch):
    env = await _services(monkeypatch)
    transmit = AsyncMock()

    async def route(_entry, _conversation, guarded_transmit):
        env.hass.auth.async_get_user.return_value = None
        return await guarded_transmit("123@c.us", "direct", False)

    async def send(_entry, _principal, _data, *, async_route):
        return await async_route(env.direct, transmit)

    env.handlers["async_media_send_route"] = route
    env.handlers["async_send_media"] = send
    result = await env.handlers["_async_handle_send_media"](env.call)
    assert result["error_code"] == "not_authorized"
    transmit.assert_not_awaited()
