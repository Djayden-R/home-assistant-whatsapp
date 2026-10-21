"""Synthetic application polls through real sends, signed callbacks and replay."""

import asyncio
import hashlib
import hmac
import json
from copy import deepcopy
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from ._loader import load_integration_module
from .test_channel_services import Store, _setup
from .test_poll_channel import manager_module

polls = load_integration_module("polls")
sending = load_integration_module("poll_send")
PHONE = "19999999999@c.us"
OTHER = "18888888888@c.us"
LID = "177777777777777@lid"
SIGNING_KEY = "synthetic-webhook-signing-key"


class Clock:
    """Run the real poll manager's scheduled callbacks without wall-clock waits."""

    def __init__(self):
        self.now = 2_000_000_000.0
        self.timers = []
        self.tasks = []

    def call_later(self, delay, callback, *args):
        timer = SimpleNamespace(
            at=self.now + delay, callback=callback, args=args, cancelled=False
        )
        timer.cancel = lambda: setattr(timer, "cancelled", True)
        self.timers.append(timer)
        return timer

    def create_task(self, _hass, coro, _name):
        task = asyncio.create_task(coro)
        self.tasks.append(task)
        return task

    async def advance(self, seconds):
        self.now += seconds
        for timer in list(self.timers):
            if timer.at <= self.now and not timer.cancelled:
                timer.cancelled = True
                timer.callback(*timer.args)
        if self.tasks:
            await asyncio.gather(*self.tasks)
            self.tasks.clear()


async def environment(monkeypatch):
    """Only HTTP/framework storage boundaries are fake; poll code is production."""
    env = await _setup(monkeypatch, observations=False)
    env.entry.subentries["contact"].data = {"recipient": PHONE.partition("@")[0]}
    contact = env.registry._contact(env.entry.subentries["contact"])
    env.direct = contact.conversation_id
    env.entry.data["webhook_secret"] = SIGNING_KEY
    env.clock = Clock()
    env.hass.loop = env.clock
    env.hass.bus.async_fire = lambda event, payload, **_kw: env.hass.bus.events.append(
        (event, deepcopy(payload))
    )
    env.entry.async_create_task = env.clock.create_task
    monkeypatch.setattr(manager_module.time, "time", lambda: env.clock.now)
    monkeypatch.setattr(manager_module, "DurableStore", lambda *_a, **_kw: Store())
    env.manager = manager_module.WahaPollManager(
        env.hass, env.entry, env.client, SIGNING_KEY, env.registry
    )
    env.entry.runtime_data.poll_manager = env.manager
    await env.manager.async_start()
    env.client.async_resolve_lid = AsyncMock(return_value=None)
    return env


async def send(env, *, suffix="ONE", settle=5):
    env.client._request.return_value = {"id": f"true_{PHONE}_POLL{suffix}"}
    result = await sending.async_send_poll_to_conversation(
        env.entry,
        conversation_id=env.direct,
        client_request_id=f"request_{suffix}",
        message="Synthetic choice",
        settle_seconds=settle,
        actions=[
            {"action": "option_yes", "title": "Yes"},
            {"action": "option_no", "title": "No"},
        ],
    )
    assert result["status"] == "accepted"
    return result


def vote(
    env, *, suffix="ONE", sender=PHONE, destination=PHONE, selected=None, timestamp=None
):
    serialized_destination = destination if "@" in destination else PHONE
    return {
        "event": "poll.vote",
        "session": "default",
        "payload": {
            "poll": {
                "id": f"true_{serialized_destination}_POLL{suffix}",
                "fromMe": True,
                "to": destination,
            },
            "vote": {
                "from": sender,
                "fromMe": False,
                "to": None,
                "timestamp": timestamp if timestamp is not None else env.clock.now,
                "selectedOptions": ["Yes"] if selected is None else selected,
            },
        },
    }


async def webhook(env, payload, *, secret=SIGNING_KEY, algorithm="sha512"):
    raw = json.dumps(payload, separators=(",", ":")).encode()
    request = SimpleNamespace(
        content=SimpleNamespace(read=AsyncMock(return_value=raw)),
        headers={
            "X-Webhook-Hmac-Algorithm": algorithm,
            "X-Webhook-Hmac": hmac.new(
                secret.encode(), raw, hashlib.sha512
            ).hexdigest(),
        },
    )
    unused = SimpleNamespace(async_handle_payload=AsyncMock())
    return await env.handlers["_async_handle_waha_webhook"](
        env.manager,
        unused,
        unused,
        unused,
        False,
        env.hass,
        "synthetic_webhook",
        request,
    )


def selections(env):
    return [
        payload
        for event, payload in env.hass.bus.events
        if event == "waha_whatsapp_event"
        and payload["type"] == "poll.selection_settled"
    ]


async def restart(env):
    env.manager.stop()
    manager = manager_module.WahaPollManager(
        env.hass, env.entry, env.client, SIGNING_KEY, env.registry
    )
    manager._store = env.manager._store
    await manager.async_start()
    env.manager = manager
    env.entry.runtime_data.poll_manager = manager


async def test_actual_send_signed_vote_settles_and_replays_unchanged_after_restart(
    monkeypatch,
):
    env = await environment(monkeypatch)
    result = await send(env)
    pending = env.manager._registry.pending(f"true_{PHONE}_POLLONE")
    assert (
        pending.chat_id
        == env.client._request.await_args.kwargs["json"]["chatId"]
        == PHONE
    )
    assert pending.origin == "application"
    assert pending.emit_legacy_action is False
    incoming = vote(env)
    assert (await webhook(env, incoming)).status == 200
    assert pending.selected_title == "Yes"
    await env.clock.advance(4)
    assert not selections(env)
    await env.clock.advance(1)
    [event] = selections(env)
    assert event["schema_version"] == 1
    assert event["conversation_id"] == env.direct
    assert event["origin"] == "application"
    assert event["client_request_id"] == "request_ONE"
    assert (
        event["vote_occurred_at"]
        == datetime.fromtimestamp(
            incoming["payload"]["vote"]["timestamp"], UTC
        ).isoformat()
    )
    assert event["selection_sequence"] == 1
    assert event["poll"] == {
        "message_id": result["message_id"],
        "selected_option": None,
        "action_id": "option_yes",
    }
    assert env.manager._store.data["selections"][0]["payload"] == event
    assert all(
        name != "mobile_app_notification_action" for name, _ in env.hass.bus.events
    )
    await restart(env)
    assert env.manager.selections(env.direct)["events"] == [event]
    assert env.manager.selections(env.direct, 1)["events"] == []
    assert env.manager.selections("other_conversation")["events"] == []
    await webhook(env, incoming)
    await env.clock.advance(10)
    assert selections(env) == [event]
    assert env.manager.rejected_vote_reasons == {"unknown_poll": 1}
    assert env.client._request.await_count == 1


@pytest.mark.parametrize("stored", ["19999999999", PHONE, "19999999999@s.whatsapp.net"])
async def test_restored_pending_compatibility_preserves_state_and_existing_journal(
    monkeypatch, stored
):
    env = await environment(monkeypatch)
    await send(env, suffix="FIRST", settle=0)
    await webhook(env, vote(env, suffix="FIRST"))
    await env.clock.advance(0)
    [previous] = selections(env)
    await send(env, suffix="SECOND")
    await webhook(env, vote(env, suffix="SECOND", timestamp=env.clock.now + 1))
    snapshot = deepcopy(env.manager._store.data)
    snapshot["polls"][0]["chat_id"] = stored
    expected = deepcopy(snapshot)
    if stored == "19999999999":
        expected["polls"][0]["chat_id"] = PHONE
    env.manager._store.data = deepcopy(snapshot)
    await restart(env)
    assert env.manager._registry.as_dict() == expected
    assert env.manager.selections(env.direct)["events"] == [previous]
    # A genuine newer vote after load replaces the old settle timer and action.
    await webhook(
        env, vote(env, suffix="SECOND", selected=["No"], timestamp=env.clock.now + 2)
    )
    await env.clock.advance(5)
    events = selections(env)
    assert events[0] == previous
    assert events[1]["poll"]["action_id"] == "option_no"
    assert events[1]["selection_sequence"] == previous["selection_sequence"] + 1
    roundtrip = polls.PollRegistry()
    roundtrip.load(deepcopy(env.manager._store.data))
    assert roundtrip.as_dict() == env.manager._store.data
    assert all(
        name != "mobile_app_notification_action" for name, _ in env.hass.bus.events
    )


@pytest.mark.parametrize(
    ("sender", "destination", "mapping"),
    [
        ("19999999999@s.whatsapp.net", PHONE, None),
        (PHONE, "19999999999@s.whatsapp.net", None),
        ("19999999999@C.US", "19999999999@S.WHATSAPP.NET", None),
        ("19999999999:3@s.whatsapp.net", "19999999999:7@c.us", None),
        (LID, LID, PHONE),
        ("177777777777777:9@lid", PHONE, PHONE),
    ],
)
async def test_verified_equivalent_identities_settle_without_losing_voter_check(
    monkeypatch, sender, destination, mapping
):
    env = await environment(monkeypatch)
    await send(env)
    env.client.async_resolve_lid.return_value = mapping
    await webhook(env, vote(env, sender=sender, destination=destination))
    await env.clock.advance(5)
    assert len(selections(env)) == 1
    if mapping:
        assert env.client.async_resolve_lid.await_count == 1
        env.client.async_resolve_lid.assert_awaited_with(LID)
    else:
        env.client.async_resolve_lid.assert_not_awaited()


@pytest.mark.parametrize(
    ("sender", "destination", "mapping", "reason"),
    [
        (OTHER, PHONE, None, "sender_identity_mismatch"),
        (PHONE, OTHER, None, "poll_chat_mismatch"),
        (PHONE, "123456789@g.us", None, "poll_chat_mismatch"),
        ("19999999999", PHONE, None, "sender_identity_mismatch"),
        (PHONE, "19999999999", None, "poll_chat_mismatch"),
        ("19999999999@c.us.evil", PHONE, None, "sender_identity_mismatch"),
        ("19999999999@c.uſ", PHONE, None, "sender_identity_mismatch"),
        (PHONE, "prefix19999999999@c.us", None, "poll_chat_mismatch"),
        ("19999999999:x@c.us", PHONE, None, "sender_identity_mismatch"),
        (LID, LID, None, "lid_resolution_failed"),
        (LID, PHONE, OTHER, "sender_identity_mismatch"),
        (PHONE, LID, OTHER, "poll_chat_mismatch"),
    ],
)
async def test_rejected_identities_never_settle_or_enter_application_journal(
    monkeypatch, sender, destination, mapping, reason
):
    env = await environment(monkeypatch)
    await send(env)
    env.client.async_resolve_lid.return_value = mapping
    await webhook(env, vote(env, sender=sender, destination=destination))
    await env.clock.advance(10)
    assert env.manager.rejected_vote_reasons == {reason: 1}
    assert not selections(env)
    assert not env.manager.selections(env.direct)["events"]
    assert env.manager.pending_count == 1


async def test_duplicate_stale_deselect_unknown_option_and_unknown_poll_are_isolated(
    monkeypatch,
):
    env = await environment(monkeypatch)
    await send(env)
    original = vote(env)
    await webhook(env, original)
    await webhook(env, original)
    await webhook(env, vote(env, timestamp=env.clock.now - 1))
    await webhook(env, vote(env, selected=["Unknown"], timestamp=env.clock.now + 1))
    await webhook(env, vote(env, suffix="UNKNOWN"))
    assert env.manager.rejected_vote_reasons == {
        "stale_vote": 2,
        "unknown_option": 1,
        "unknown_poll": 1,
    }
    await webhook(env, vote(env, selected=[], timestamp=env.clock.now + 2))
    await env.clock.advance(10)
    assert not selections(env)
    pending = env.manager._registry.pending("POLLONE")
    assert pending.selected_title is None
    assert pending.commit_at is None
    await webhook(env, vote(env, selected=["No"], timestamp=env.clock.now + 3))
    await env.clock.advance(5)
    assert selections(env)[0]["poll"]["action_id"] == "option_no"
    assert len(env.manager.selections(env.direct)["events"]) == 1


@pytest.mark.parametrize(
    "change", ["signature", "algorithm", "session", "poll_direction", "vote_direction"]
)
async def test_signed_dispatcher_keeps_authentication_session_and_direction_guards(
    monkeypatch, change
):
    env = await environment(monkeypatch)
    await send(env)
    incoming = vote(env)
    kwargs = {}
    if change == "signature":
        kwargs["secret"] = "synthetic-other-entry-key"
    elif change == "algorithm":
        kwargs["algorithm"] = "sha256"
    elif change == "session":
        incoming["session"] = "other_session"
    elif change == "poll_direction":
        incoming["payload"]["poll"]["fromMe"] = False
    else:
        incoming["payload"]["vote"]["fromMe"] = True
    response = await webhook(env, incoming, **kwargs)
    assert response.status == (401 if change in ("signature", "algorithm") else 200)
    await env.clock.advance(10)
    assert not selections(env)
    assert env.manager._registry.pending("POLLONE").latest_vote_timestamp is None


async def test_cross_entry_signed_callback_and_conversation_remain_isolated(
    monkeypatch,
):
    first = await environment(monkeypatch)
    await send(first)
    other = await environment(monkeypatch)
    other.entry.entry_id = "other_entry"
    other.entry.data["session"] = "other_session"
    other.client.session_name = "other_session"
    other.manager._session_name = "other_session"
    other.manager._webhook_secret = "synthetic-other-entry-key"
    # Neither possession of the first poll ID nor its valid first-entry HMAC
    # authorizes a submission to a different integration entry.
    assert (await webhook(other, vote(first))).status == 401
    assert (
        await webhook(other, vote(first), secret="synthetic-other-entry-key")
    ).status == 200
    assert other.manager.rejected_vote_reasons == {"invalid_event_or_session": 1}
    assert not other.manager._registry.pending_polls()
    assert not selections(other)
    assert other.manager.selections(first.direct)["events"] == []
    assert first.manager._registry.pending("POLLONE").latest_vote_timestamp is None


async def test_selection_journal_bound_and_restore_dedup_keep_sequence_stable(
    monkeypatch,
):
    env = await environment(monkeypatch)
    await send(env, settle=0)
    await webhook(env, vote(env))
    await env.clock.advance(0)
    [original] = selections(env)
    # Exercise the retained journal bound without sending thousands of polls.
    for index in range(2048):
        event = {**deepcopy(original), "event_id": f"event_synthetic_{index}"}
        env.manager._registry.remember_selection(event)
    snapshot = env.manager._registry.as_dict()
    assert snapshot["selection_sequence"] == 2049
    assert len(snapshot["selections"]) == 2048
    assert snapshot["selections"][0]["payload"]["selection_sequence"] == 2
    snapshot["selections"].append(deepcopy(snapshot["selections"][-1]))
    restored = polls.PollRegistry()
    restored.load(snapshot)
    assert restored.as_dict()["selection_sequence"] == 2049
    assert len(restored.as_dict()["selections"]) == 2047
    page = restored.selections(env.direct, 2047, 100)
    assert [event["selection_sequence"] for event in page["events"]] == [2048, 2049]
    assert page["next_sequence"] == 2049
    assert page["has_more"] is False


def test_only_known_application_bare_destination_is_repaired_on_restore():
    base = {
        "message_id": "true_19999999999@c.us_SYNTHETIC",
        "chat_id": "19999999999",
        "options": {"Yes": "option_yes", "No": "option_no"},
        "settle_seconds": 5,
        "origin": "application",
        "client_request_id": "request_example",
        "conversation_id": "direct_example",
        "emit_legacy_action": False,
        "latest_vote_timestamp": 123.0,
        "selected_title": "Yes",
        "commit_at": 128.0,
    }
    restored = polls.PendingPoll.from_dict(base)
    assert restored.chat_id == PHONE
    assert restored.as_dict() == {
        **base,
        "message_id": "SYNTHETIC",
        "chat_id": PHONE,
        "person_entity_id": None,
    }
    for malformed in (
        "+19999999999",
        " 19999999999",
        "１９９９９９９９９９９",
        "199999",
        "1999999999999999",
        "19999999999@lid",
        "123456789@g.us",
    ):
        pending = polls.PendingPoll.from_dict({**base, "chat_id": malformed})
        # Preserve the old record without granting any unsupported vote identity.
        assert pending.chat_id == malformed
        registry = polls.PollRegistry()
        registry.load({"polls": [pending.as_dict()]})
        result = registry.apply_vote_with_reason(
            polls.PollVote("SYNTHETIC", malformed, malformed, "Yes", 124), 124
        )
        assert result is polls.PollVoteRejectionReason.POLL_CHAT_MISMATCH
    legacy = polls.PendingPoll.from_dict(
        {
            **base,
            "origin": "automation",
            "client_request_id": None,
            "emit_legacy_action": True,
        }
    )
    assert legacy.chat_id == base["chat_id"]
