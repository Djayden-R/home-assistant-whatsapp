"""Synthetic exact-read provenance and adversarial ephemeral activity lifetimes."""

import asyncio
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock, call

import pytest

from ._loader import load_integration_module

activity = load_integration_module("activity")
registry_module = load_integration_module("channel_registry")
api = load_integration_module("api")

ACCOUNT = "19999999999@c.us"
PARTICIPANT = "18888888888@c.us"
GROUP = "123456789@g.us"


class Store:
    def __init__(self):
        self.data = None

    async def async_load(self):
        return deepcopy(self.data)

    async def async_save(self, data):
        self.data = deepcopy(data)


class Clock:
    """Run native timer callbacks deterministically, including cancelled races."""

    def __init__(self):
        self.now = 0
        self.timers = []

    def schedule(self, delay, callback):
        timer = SimpleNamespace(at=self.now + delay, callback=callback, cancelled=False)
        self.timers.append(timer)

        def cancel():
            timer.cancelled = True

        return cancel

    async def advance(self, seconds):
        self.now += seconds
        for timer in list(self.timers):
            if timer.at <= self.now and not timer.cancelled:
                timer.cancelled = True
                await timer.callback(None)


async def environment(monkeypatch):
    monkeypatch.setattr(registry_module, "_notify_entity_id", lambda *_args: None)
    entry = SimpleNamespace(
        entry_id="entry_example",
        state="loaded",
        options={},
        data={"session": "example", "webhook_secret": "synthetic-secret"},
        subentries={
            name: SimpleNamespace(
                subentry_id=name, subentry_type="recipient", data={"recipient": number}
            )
            for name, number in (("contact", "17777777777"), ("other", "16666666666"))
        },
    )
    client = SimpleNamespace(
        session_name="example",
        async_get_server=AsyncMock(
            return_value=SimpleNamespace(version="2026.9.1", engine="GOWS")
        ),
        async_get_session=AsyncMock(
            return_value=SimpleNamespace(
                name="example", status="WORKING", account_id=ACCOUNT
            )
        ),
        async_mark_seen=AsyncMock(),
        async_set_presence=AsyncMock(),
    )
    hass = SimpleNamespace(data={})
    store = Store()
    registry = registry_module.ChannelRegistry(hass, entry, client, store=store)
    await registry.async_start()
    routes = {}
    general = {}
    members = {
        PARTICIPANT: {
            "participant_id": "participant_example",
            "membership_id": "membership_example",
        }
    }
    guests = SimpleNamespace(
        resolve_route=lambda conv: routes.get(conv),
        resolve_group_conversation=lambda conv: (
            GROUP if conv == "guest_group" else None
        ),
        resolve_member=lambda sender, **_kw: members.get(sender),
    )
    groups = SimpleNamespace(
        resolve_conversation=lambda conv: general.get(conv),
        async_verify=AsyncMock(
            return_value=SimpleNamespace(
                identity=lambda sender: sender if sender == PARTICIPANT else None
            )
        ),
    )
    registry.set_extra_route_resolver(
        lambda conv: (
            routes.get(conv)
            or (GROUP if conv in ("guest_group", "general_group") else None)
        )
    )
    guest_manager = SimpleNamespace(
        async_reconcile=AsyncMock(return_value={"ready": True}),
        async_verify_group_destination=AsyncMock(return_value=True),
        async_verify_guest_destination=AsyncMock(return_value=True),
    )
    runtime = SimpleNamespace(
        client=client,
        session=SimpleNamespace(account_id=ACCOUNT),
        server=client.async_get_server.return_value,
        channel_registry=registry,
        general_groups=groups,
        guest_registry=guests,
        guest_manager=guest_manager,
    )
    entry.runtime_data = runtime
    clock = Clock()
    manager = activity.ActivityManager(hass, entry, schedule=clock.schedule)
    runtime.activity_manager = manager
    direct = registry.resolve_conversation(
        registry._conversation_id("contact", "17777777777")
    ).conversation_id
    other = registry._conversation_id("other", "16666666666")

    def typing(
        activity_id="activity_01", *, state="typing", conversation=direct, **extra
    ):
        return {
            "config_entry_id": entry.entry_id,
            "conversation_id": conversation,
            "activity_id": activity_id,
            "state": state,
            **extra,
        }

    async def remember(
        *, conversation=direct, raw_id=None, participant=None, incoming=True
    ):
        _, destination, _ = activity._route(entry, conversation, participant)
        raw_id = raw_id or f"false_{destination}_EXAMPLE01" + (
            f"_{participant}" if participant else ""
        )
        kwargs = (
            activity.incoming_activity_provenance(
                entry, conversation, raw_id, destination, raw_participant=participant
            )
            if incoming
            else {}
        )
        token = await registry.async_remember_message(raw_id, conversation, **kwargs)
        return {
            "config_entry_id": entry.entry_id,
            "conversation_id": conversation,
            "message_id": token,
        }

    return SimpleNamespace(**locals(), principal=AsyncMock())


@pytest.mark.parametrize(
    "patch",
    [
        {"ttl_seconds": True},
        {"ttl_seconds": False},
        {"ttl_seconds": "10"},
        {"ttl_seconds": 10.0},
        {"ttl_seconds": 2},
        {"ttl_seconds": 16},
        {"activity_id": "é"},
        {"activity_id": "_bad"},
        {"activity_id": "a" * 129},
        {"activity_id": "a\n"},
        {"activity_id": 3},
        {"state": "online"},
        {"conversation_id": ""},
        {"conversation_id": "a" * 257},
        {"config_entry_id": 42},
        {"unknown": "field"},
        {"state": "paused", "ttl_seconds": 10},
    ],
)
async def test_strict_fields_refused_without_network_effects(monkeypatch, patch):
    env = await environment(monkeypatch)
    result = await env.manager.async_set_typing(
        {**env.typing(), **patch}, env.principal
    )
    assert result["error_code"] == "invalid_request"
    assert result["schema_version"] == 1
    assert result["applied"] is False
    env.client.async_get_server.assert_not_awaited()
    env.client.async_set_presence.assert_not_awaited()


@pytest.mark.parametrize(
    ("engine", "version", "ready"),
    [
        ("GOWS", "2026.9.1", True),
        ("GOWS", "2026.10.2+dev", True),
        ("GOWS", "2026.8.2", False),
        ("NOWEB", "2026.9.1", False),
        ("WEBJS", "2026.9.1", False),
        ("GOWS", "future", False),
    ],
)
async def test_capabilities_are_verified_and_conservative(
    monkeypatch, engine, version, ready
):
    env = await environment(monkeypatch)
    env.client.async_get_server.return_value = SimpleNamespace(
        engine=engine, version=version
    )
    assert await env.manager.capabilities() == {
        "schema_version": 1,
        "mark_read": ready,
        "typing": ready,
        "read_scope": "message",
    }
    request = await env.remember()
    result = await env.manager.async_mark_read(request, env.principal)
    assert result["status"] == ("accepted" if ready else "failed")
    if not ready:
        assert result["error_code"] == "unsupported"
        env.client.async_mark_seen.assert_not_awaited()


async def test_exact_repeated_reads_have_no_send_observation_or_ledger(monkeypatch):
    env = await environment(monkeypatch)
    request = await env.remember()
    for _ in range(2):
        assert await env.manager.async_mark_read(request, env.principal) == {
            "schema_version": 1,
            "conversation_id": env.direct,
            "message_id": request["message_id"],
            "status": "accepted",
        }
    assert (
        env.client.async_mark_seen.await_args_list
        == [
            call(
                "17777777777@c.us", "false_17777777777@c.us_EXAMPLE01", participant=None
            )
        ]
        * 2
    )
    env.client.async_set_presence.assert_not_awaited()


@pytest.mark.parametrize(
    "kind",
    ["outgoing", "legacy", "cross_conversation", "cross_entry", "expired", "unknown"],
)
async def test_read_requires_exact_incoming_ownership_and_retention(monkeypatch, kind):
    env = await environment(monkeypatch)
    request = await env.remember(incoming=kind not in ("outgoing", "legacy"))
    if kind == "cross_conversation":
        request["conversation_id"] = env.other
    elif kind == "cross_entry":
        request["config_entry_id"] = "other_entry"
    elif kind == "expired":
        env.registry._messages[request["message_id"]].expires_at = 0
    elif kind == "unknown":
        request["message_id"] = "raw_address@c.us"
    result = await env.manager.async_mark_read(request, env.principal)
    assert result["status"] == "failed"
    assert result["error_code"] in ("invalid_message", "invalid_conversation")
    env.client.async_mark_seen.assert_not_awaited()


@pytest.mark.parametrize(
    "raw_id",
    [
        "true_17777777777@c.us_EXAMPLE01",
        "false_15555555555@c.us_EXAMPLE01",
        "false_123456789@g.us_EXAMPLE01",
        "unqualified-legacy-id",
        "false_17777777777@c.us_EXAMPLE01_extra",
        "false_17777777777@c.us_",
    ],
)
async def test_serialized_key_cannot_override_observed_direction_or_destination(
    monkeypatch, raw_id
):
    env = await environment(monkeypatch)
    request = await env.remember(raw_id=raw_id)
    assert env.registry.resolve_message(request["message_id"], env.direct) == raw_id
    result = await env.manager.async_mark_read(request, env.principal)
    assert result["error_code"] == "invalid_message"
    env.client.async_mark_seen.assert_not_awaited()


async def test_quote_snapshot_restores_incoming_provenance_but_preserves_legacy(
    monkeypatch,
):
    env = await environment(monkeypatch)
    request = await env.remember()
    legacy = await env.remember(raw_id="legacy-quote", incoming=False)
    restored = registry_module.ChannelRegistry(
        env.hass, env.entry, env.client, store=env.store
    )
    await restored.async_start()
    assert restored.resolve_incoming_message(request["message_id"], env.direct) == (
        "false_17777777777@c.us_EXAMPLE01",
        activity.activity_observation_binding(env.entry, env.direct),
        None,
    )
    assert restored.resolve_message(legacy["message_id"], env.direct) == "legacy-quote"
    assert restored.resolve_incoming_message(legacy["message_id"], env.direct) is None


@pytest.mark.parametrize("route", ["general_group", "guest_group", "guest_direct"])
async def test_configured_and_guest_routes_read_and_type_exact_destinations(
    monkeypatch, route
):
    env = await environment(monkeypatch)
    env.general["general_group"] = SimpleNamespace(
        subentry_id="group", group_jid=GROUP, bot_account=ACCOUNT
    )
    env.routes["guest_direct"] = PARTICIPANT
    participant = None if route == "guest_direct" else PARTICIPANT
    request = await env.remember(conversation=route, participant=participant)
    assert (await env.manager.async_mark_read(request, env.principal))[
        "status"
    ] == "accepted"
    destination = PARTICIPANT if route == "guest_direct" else GROUP
    key = f"false_{destination}_EXAMPLE01" + (f"_{participant}" if participant else "")
    env.client.async_mark_seen.assert_awaited_once_with(
        destination, key, participant=participant
    )
    assert (
        await env.manager.async_set_typing(
            env.typing(conversation=route), env.principal
        )
    )["applied"] is True
    await env.clock.advance(10)
    assert env.client.async_set_presence.await_args_list == [
        call(destination, "typing"),
        call(destination, "paused"),
        call(None, "offline"),
    ]


async def test_group_participant_mismatch_is_quote_only(monkeypatch):
    env = await environment(monkeypatch)
    request = await env.remember(
        conversation="guest_group",
        participant=PARTICIPANT,
        raw_id=f"false_{GROUP}_EXAMPLE01_14444444444@c.us",
    )
    assert (await env.manager.async_mark_read(request, env.principal))[
        "error_code"
    ] == "invalid_message"
    env.client.async_mark_seen.assert_not_awaited()


async def test_unknown_route_refused_before_backend_read_or_effect(monkeypatch):
    env = await environment(monkeypatch)
    result = await env.manager.async_set_typing(
        env.typing(conversation="15555555555@c.us"), env.principal
    )
    assert result["error_code"] == "invalid_conversation"
    env.client.async_get_server.assert_not_awaited()
    env.client.async_set_presence.assert_not_awaited()


async def test_refresh_pause_and_expiry_never_revive_or_clear_new_lifetime(monkeypatch):
    env = await environment(monkeypatch)
    assert (await env.manager.async_set_typing(env.typing("a"), env.principal))[
        "applied"
    ]
    old_timer = env.clock.timers[-1]
    assert (
        await env.manager.async_set_typing(
            env.typing("a", ttl_seconds=15), env.principal
        )
    )["applied"]
    refresh_timer = env.clock.timers[-1]
    await env.clock.advance(8)
    assert (await env.manager.async_set_typing(env.typing("b"), env.principal))[
        "applied"
    ]
    writes = env.client.async_set_presence.await_count
    for state in ("typing", "paused"):
        assert (
            await env.manager.async_set_typing(
                env.typing("a", state=state), env.principal
            )
        )["applied"] is False
    await old_timer.callback(None)
    await refresh_timer.callback(None)
    assert env.client.async_set_presence.await_count == writes
    await env.clock.advance(10)
    assert env.client.async_set_presence.await_args_list[-2:] == [
        call("17777777777@c.us", "paused"),
        call(None, "offline"),
    ]
    assert (await env.manager.async_set_typing(env.typing("b"), env.principal))[
        "applied"
    ] is False


async def test_pause_before_delayed_start_retires_the_identity(monkeypatch):
    env = await environment(monkeypatch)
    pause = env.typing("delayed", state="paused")
    assert (await env.manager.async_set_typing(pause, env.principal))[
        "applied"
    ] is False
    assert (await env.manager.async_set_typing(env.typing("delayed"), env.principal))[
        "applied"
    ] is False
    env.client.async_set_presence.assert_not_awaited()


async def test_session_offline_waits_for_other_conversation_lease(monkeypatch):
    env = await environment(monkeypatch)
    await env.manager.async_set_typing(env.typing("a"), env.principal)
    await env.manager.async_set_typing(
        env.typing("b", conversation=env.other), env.principal
    )
    await env.manager.async_set_typing(env.typing("a", state="paused"), env.principal)
    assert call(None, "offline") not in env.client.async_set_presence.await_args_list
    await env.manager.async_set_typing(
        env.typing("b", state="paused", conversation=env.other), env.principal
    )
    assert env.client.async_set_presence.await_args_list[-1] == call(None, "offline")


@pytest.mark.parametrize(
    "failure",
    [
        api.WahaConnectionError("private provider detail"),
        api.WahaResponseError("private invalid JSON"),
        api.WahaRequestError("private upstream", 503),
    ],
)
async def test_lost_typing_response_is_unknown_and_lease_still_expires(
    monkeypatch, failure
):
    env = await environment(monkeypatch)
    env.client.async_set_presence.side_effect = [failure, None, None]
    result = await env.manager.async_set_typing(env.typing(), env.principal)
    assert result == {
        "schema_version": 1,
        "conversation_id": env.direct,
        "activity_id": "activity_01",
        "status": "unknown",
        "applied": None,
        "error_code": "outcome_unknown",
    }
    assert env.client.async_set_presence.await_count == 1
    await env.clock.advance(10)
    assert env.client.async_set_presence.await_count == 3
    assert not env.manager._current


@pytest.mark.parametrize(
    ("failure", "code", "status"),
    [
        (api.WahaConnectionError("private"), "outcome_unknown", "unknown"),
        (api.WahaRequestError("private", 429), "rate_limited", "failed"),
        (api.WahaRequestError("private", 400), "transport_failure", "failed"),
        (api.WahaAuthenticationError("private"), "channel_unavailable", "failed"),
    ],
)
async def test_read_transport_outcome_classification_has_no_retry_or_detail(
    monkeypatch, failure, code, status
):
    env = await environment(monkeypatch)
    request = await env.remember()
    env.client.async_mark_seen.side_effect = failure
    response = await env.manager.async_mark_read(request, env.principal)
    assert response["status"] == status
    assert response["error_code"] == code
    assert "private" not in str(response)
    env.client.async_mark_seen.assert_awaited_once()


async def test_lost_capability_probe_does_not_remove_owned_expiry(monkeypatch):
    env = await environment(monkeypatch)
    await env.manager.async_set_typing(env.typing(), env.principal)
    env.client.async_get_server.side_effect = api.WahaConnectionError("lost GET")
    assert (await env.manager.capabilities())["typing"] is False
    assert env.manager._current
    assert not env.clock.timers[-1].cancelled
    env.client.async_get_server.side_effect = None
    await env.clock.advance(10)
    assert env.client.async_set_presence.await_args_list[-1] == call(None, "offline")


async def test_cancelled_start_retains_expiry_and_cancelled_pause_keeps_cleanup(
    monkeypatch,
):
    env = await environment(monkeypatch)
    entered = asyncio.Event()
    release = asyncio.Event()

    async def pending(*_args):
        entered.set()
        await release.wait()

    env.client.async_set_presence.side_effect = pending
    task = asyncio.create_task(
        env.manager.async_set_typing(env.typing(), env.principal)
    )
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert env.manager._current
    entered.clear()
    task = asyncio.create_task(
        env.manager.async_set_typing(env.typing(state="paused"), env.principal)
    )
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert env.manager._current
    env.client.async_set_presence.side_effect = None
    await env.clock.advance(10)
    assert env.client.async_set_presence.await_args_list[-1] == call(None, "offline")


async def test_disconnect_is_serialized_with_inflight_start_and_does_not_replay(
    monkeypatch,
):
    env = await environment(monkeypatch)
    entered, release = asyncio.Event(), asyncio.Event()

    async def pending(*_args):
        entered.set()
        await release.wait()

    env.client.async_set_presence.side_effect = pending
    start = asyncio.create_task(
        env.manager.async_set_typing(env.typing("a"), env.principal)
    )
    await entered.wait()
    status = asyncio.create_task(
        env.manager.async_handle_session_status(
            {"session": "example", "payload": {"status": "STOPPED"}}
        )
    )
    await asyncio.sleep(0)
    assert not status.done()
    release.set()
    await start
    await status
    assert not env.manager._current
    env.client.async_set_presence.side_effect = None
    assert (await env.manager.async_set_typing(env.typing("a"), env.principal))[
        "applied"
    ] is False
    assert env.client.async_set_presence.await_count == 1
    await env.clock.advance(20)
    assert env.client.async_set_presence.await_count == 1


@pytest.mark.parametrize("change", ["route", "entry", "account", "principal"])
async def test_preflight_awaits_cannot_retain_revoked_authorization(
    monkeypatch, change
):
    env = await environment(monkeypatch)

    async def revalidate():
        if change == "route":
            env.entry.subentries["contact"].data["recipient"] = "15555555555"
        elif change == "entry":
            env.entry.runtime_data = SimpleNamespace()
        elif change == "account":
            env.runtime.session.account_id = "12222222222@c.us"
        else:
            raise activity.ActivityError("not_authorized")

    calls = 0

    async def principal():
        nonlocal calls
        calls += 1
        if calls == 2:
            await revalidate()

    result = await env.manager.async_set_typing(env.typing(), principal)
    assert result["status"] == "failed"
    env.client.async_set_presence.assert_not_awaited()


async def test_unload_cleans_each_owned_chat_and_only_then_global_offline(monkeypatch):
    env = await environment(monkeypatch)
    await env.manager.async_set_typing(env.typing("a"), env.principal)
    await env.manager.async_set_typing(
        env.typing("b", conversation=env.other), env.principal
    )
    env.entry.state = "unloading"
    await env.manager.async_stop()
    assert env.client.async_set_presence.await_args_list[-3:] == [
        call("17777777777@c.us", "paused"),
        call("16666666666@c.us", "paused"),
        call(None, "offline"),
    ]
    writes = env.client.async_set_presence.await_count
    await env.clock.advance(20)
    await env.manager.async_stop()
    assert env.client.async_set_presence.await_count == writes
    assert (await env.manager.async_set_typing(env.typing("c"), env.principal))[
        "error_code"
    ] == "channel_unavailable"


async def test_account_rebind_never_targets_replacement_account_during_cleanup(
    monkeypatch,
):
    env = await environment(monkeypatch)
    await env.manager.async_set_typing(env.typing(), env.principal)
    env.runtime.session.account_id = "12222222222@c.us"
    env.client.async_get_session.return_value.account_id = "12222222222@c.us"
    await env.clock.advance(10)
    assert env.client.async_set_presence.await_count == 1
    assert not env.manager._current


async def test_retired_identity_and_conversation_bounds_refuse_new_ids_without_eviction(
    monkeypatch,
):
    env = await environment(monkeypatch)
    monkeypatch.setattr(activity, "MAX_ACTIVITY_IDS", 2)
    monkeypatch.setattr(activity, "MAX_ACTIVITY_CONVERSATIONS", 1)
    await env.manager.async_set_typing(env.typing("a", state="paused"), env.principal)
    await env.manager.async_set_typing(env.typing("b"), env.principal)
    assert (await env.manager.async_set_typing(env.typing("c"), env.principal))[
        "error_code"
    ] == "rate_limited"
    assert (
        await env.manager.async_set_typing(
            env.typing("d", conversation=env.other), env.principal
        )
    )["error_code"] == "rate_limited"
    assert (await env.manager.async_set_typing(env.typing("a"), env.principal))[
        "applied"
    ] is False
    assert len(env.manager._history) == 2
    assert env.manager._current[env.direct].activity_id == "b"


@pytest.mark.parametrize("discover", ["capability", "status", "typing"])
async def test_first_connection_binds_initially_unbound_account_once(
    monkeypatch, discover
):
    env = await environment(monkeypatch)
    env.runtime.session.account_id = None
    env.manager = activity.ActivityManager(
        env.hass, env.entry, schedule=env.clock.schedule
    )
    env.runtime.activity_manager = env.manager
    if discover == "capability":
        assert (await env.manager.capabilities())["typing"] is True
    elif discover == "status":
        await env.manager.async_handle_session_status(
            {"session": "example", "payload": {"status": "WORKING"}}
        )
    else:
        assert (
            await env.manager.async_set_typing(env.typing("initial"), env.principal)
        )["status"] == "accepted"
    assert env.manager._account == ACCOUNT
    assert env.runtime.session.account_id is None
    request = await env.remember()
    assert (await env.manager.async_mark_read(request, env.principal))[
        "status"
    ] == "accepted"
    env.client.async_get_session.return_value.account_id = "12222222222@c.us"
    writes = env.client.async_set_presence.await_count
    assert (await env.manager.capabilities())["typing"] is False
    assert (await env.manager.async_set_typing(env.typing("swapped"), env.principal))[
        "error_code"
    ] == "channel_unavailable"
    assert env.client.async_set_presence.await_count == writes
    assert env.manager._account == ACCOUNT


async def test_restored_malformed_read_key_cannot_override_route(monkeypatch):
    env = await environment(monkeypatch)
    request = await env.remember()
    env.registry._messages[
        request["message_id"]
    ].incoming_read_id = "false_15555555555@c.us_EXAMPLE01"
    assert (await env.manager.async_mark_read(request, env.principal))[
        "error_code"
    ] == "invalid_message"
    env.client.async_mark_seen.assert_not_awaited()


async def test_read_rechecks_account_after_principal_await(monkeypatch):
    env = await environment(monkeypatch)
    request = await env.remember()
    count = 0

    async def principal():
        nonlocal count
        count += 1
        if count == 2:
            env.client.async_get_session.return_value.account_id = "12222222222@c.us"

    assert (await env.manager.async_mark_read(request, principal))[
        "error_code"
    ] == "channel_unavailable"
    env.client.async_mark_seen.assert_not_awaited()
