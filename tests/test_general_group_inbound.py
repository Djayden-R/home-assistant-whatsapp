"""Explicit group normalization never grants private identity or executes commands."""

import sys
from copy import deepcopy
from datetime import UTC, datetime
from time import time
from types import ModuleType, SimpleNamespace

import pytest

from ._loader import load_integration_module
from .test_general_groups import BOT, GROUP, LID, MEMBER, OTHER_GROUP, make_manager


# Match the small core contract used by inbound.py without requiring HA.
class _Context:
    def __init__(self, *, user_id):
        self.user_id = user_id


previous_core = sys.modules.get("homeassistant.core")
if previous_core is None:
    stub = ModuleType("homeassistant.core")
    stub.Context = _Context
    sys.modules["homeassistant.core"] = stub
try:
    inbound = load_integration_module("general_group_inbound")
finally:
    if previous_core is None:
        del sys.modules["homeassistant.core"]


def envelope(**payload):
    return {
        "event": "message",
        "id": "callback-a",
        "session": "house",
        "payload": {
            "id": "message-a",
            "from": GROUP,
            "participant": LID,
            "fromMe": False,
            "timestamp": time(),
            "hasMedia": False,
            "body": "Group text /registered-command",
            **payload,
        },
    }


def make_inbound():
    manager, entry, client, registry = make_manager()
    events = []
    hass = SimpleNamespace(
        bus=SimpleNamespace(async_fire=lambda *args: events.append(args[1]))
    )
    normalizer = inbound.WahaGeneralGroupInboundManager(hass, entry, manager, registry)
    return normalizer, manager, entry, client, registry, events


@pytest.mark.asyncio
async def test_group_sender_stays_separate_without_private_route():
    normalizer, _manager, _entry, _client, _registry, events = make_inbound()
    assert await normalizer.async_handle_payload(envelope()) is True
    event = events[0]
    assert event["type"] == "message.received"
    assert event["conversation_type"] == "group"
    assert event["conversation_id"].startswith("general_group_")
    assert event["sender"]["participant_id"].startswith("participant_")
    assert event["sender"]["identity_status"] == "verified"
    assert set(event["sender"]) == {"participant_id", "identity_status"}
    assert event["message"]["mentions"] == {
        "available": False,
        "bot": None,
        "participant_ids": [],
    }
    assert "@" not in str(event)
    assert len(events) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("participant", [None, "999999999@lid", "Display Name"])
async def test_unresolved_participant_has_no_opaque_identity_or_private_access(
    participant,
):
    normalizer, _manager, _entry, _client, _registry, events = make_inbound()
    assert await normalizer.async_handle_payload(envelope(participant=participant))
    assert events[0]["sender"] == {
        "participant_id": None,
        "identity_status": "unresolved",
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "change",
    [
        "wrong_session",
        "echo",
        "stale",
        "future",
        "unconfigured",
        "disabled",
        "swapped_account",
        "bot_left",
        "conflicting_sender",
        "conflicting_group",
    ],
)
async def test_invalid_or_ineligible_callbacks_publish_nothing(change):
    normalizer, _manager, entry, client, _registry, events = make_inbound()
    data = envelope()
    if change == "wrong_session":
        data["session"] = "other"
    elif change == "echo":
        data["payload"]["fromMe"] = True
    elif change == "stale":
        data["payload"]["timestamp"] = time() - 3601
    elif change == "future":
        data["payload"]["timestamp"] = time() + 301
    elif change == "unconfigured":
        data["payload"]["from"] = OTHER_GROUP
    elif change == "disabled":
        entry.subentries["group-contact"].data["enabled"] = False
    elif change == "swapped_account":
        client.account = MEMBER
    elif change == "bot_left":
        client.roster[0]["role"] = "left"
    elif change == "conflicting_sender":
        data["payload"].update({"chatId": GROUP, "from": BOT})
    else:
        data["payload"]["chatId"] = OTHER_GROUP
    assert await normalizer.async_handle_payload(data) is False
    assert events == []


@pytest.mark.asyncio
async def test_roster_is_fresh_on_each_callback_and_duplicate_is_suppressed():
    normalizer, _manager, _entry, client, _registry, events = make_inbound()
    data = envelope()
    assert await normalizer.async_handle_payload(data)
    assert await normalizer.async_handle_payload(deepcopy(data)) is False
    assert sum(path.endswith("/participants/v2") for _, path, _ in client.requests) == 2
    assert len(events) == 1


@pytest.mark.asyncio
async def test_out_of_order_callbacks_preserve_timestamps_and_suppress_replays(
    monkeypatch,
):
    normalizer, _manager, _entry, _client, _registry, events = make_inbound()
    now = int(time())
    monkeypatch.setattr(inbound, "time", SimpleNamespace(time=lambda: now))
    newer = envelope(id="newer-message", timestamp=now - 10)
    newer["id"] = "callback-newer"
    older = envelope(id="older-message", timestamp=now - 20)
    older["id"] = "callback-older"
    reaction = envelope(
        id="reaction-message",
        timestamp=now - 30,
        reaction={"text": "👍", "messageId": "older-message"},
    )
    reaction.update({"event": "message.reaction", "id": "callback-reaction"})

    for data in (newer, older, reaction):
        assert await normalizer.async_handle_payload(data)
    assert [event["type"] for event in events] == [
        "message.received",
        "message.received",
        "reaction.added",
    ]
    assert [event["occurred_at"] for event in events] == [
        datetime.fromtimestamp(now - offset, tz=UTC).isoformat()
        for offset in (10, 20, 30)
    ]
    assert len({event["event_id"] for event in events}) == 3
    assert await normalizer.async_handle_payload(deepcopy(older)) is False
    assert await normalizer.async_handle_payload(deepcopy(reaction)) is False
    stale = envelope(id="stale-message", timestamp=now - 3601)
    stale["id"] = "callback-stale"
    assert await normalizer.async_handle_payload(stale) is False
    assert len(events) == 3


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["disable", "remove", "rebind", "group"])
@pytest.mark.parametrize(
    "storage_operation", ["async_remember_message", "async_claim_event"]
)
async def test_config_changes_during_persistence_revoke_publication(
    monkeypatch, change, storage_operation
):
    normalizer, _manager, entry, _client, registry, events = make_inbound()
    original = getattr(registry, storage_operation)

    async def persist_then_revoke(*args):
        result = await original(*args)
        if change == "disable":
            entry.subentries["group-contact"].data["enabled"] = False
        elif change == "remove":
            entry.subentries.clear()
        elif change == "rebind":
            entry.subentries["group-contact"].data["bot_account"] = MEMBER
        else:
            entry.subentries["group-contact"].data["group_id"] = OTHER_GROUP
        return result

    monkeypatch.setattr(registry, storage_operation, persist_then_revoke)
    assert await normalizer.async_handle_payload(envelope()) is False
    assert events == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "mentions", [[], [BOT], [BOT.replace("@c.us", "@s.whatsapp.net"), LID, MEMBER]]
)
async def test_gows_context_mentions_are_normalized_and_deduplicated(mentions):
    normalizer, manager, _entry, _client, _registry, events = make_inbound()
    raw = {
        "Message": {"extendedTextMessage": {"contextInfo": {"mentionedJID": mentions}}}
    }
    assert await normalizer.async_handle_payload(envelope(_data=raw))
    metadata = events[0]["message"]["mentions"]
    assert metadata["available"] is True
    assert metadata["bot"] is bool(mentions)
    assert len(metadata["participant_ids"]) == (
        2 if len(mentions) == 3 else len(mentions)
    )
    assert all(pid.startswith("participant_") for pid in metadata["participant_ids"])
    assert (
        manager.resolve_subentry("group-contact").conversation_id
        == events[0]["conversation_id"]
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "value", [None, "not a list", ["unknown@lid"], ["999999999@lid"], [None]]
)
async def test_missing_malformed_or_unresolved_mentions_are_explicitly_unavailable(
    value,
):
    normalizer, _manager, _entry, _client, _registry, events = make_inbound()
    raw = {"Message": {"extendedTextMessage": {"contextInfo": {"mentionedJID": value}}}}
    assert await normalizer.async_handle_payload(
        envelope(_data=raw, mentionedIds=[BOT])
    )
    assert events[0]["message"]["mentions"] == {
        "available": False,
        "bot": None,
        "participant_ids": [],
    }


@pytest.mark.asyncio
async def test_group_quotes_and_reactions_never_resolve_another_conversation():
    normalizer, _manager, _entry, _client, registry, events = make_inbound()
    registry.set_extra_route_resolver(
        lambda route: OTHER_GROUP if route == "other" else GROUP
    )
    foreign = await registry.async_remember_message("foreign-message", "other")
    data = envelope(replyTo={"id": "foreign-message"})
    assert await normalizer.async_handle_payload(data)
    assert events[0]["message"]["in_reply_to"] is None
    assert registry.resolve_message(foreign, events[0]["conversation_id"]) is None
    reaction = envelope(
        id="reaction-a", reaction={"text": "👍", "messageId": "foreign-message"}
    )
    reaction.update({"id": "callback-reaction", "event": "message.reaction"})
    assert await normalizer.async_handle_payload(reaction)
    assert events[1]["reaction"]["target_known"] is False


@pytest.mark.asyncio
async def test_group_quote_known_in_same_conversation_is_retained():
    normalizer, manager, _entry, _client, registry, events = make_inbound()
    route = manager.resolve_subentry("group-contact").conversation_id
    token = await registry.async_remember_message("group-quote", route)
    assert await normalizer.async_handle_payload(
        envelope(replyTo={"id": "group-quote"})
    )
    assert events[0]["message"]["in_reply_to"] == token
