"""Application correlation, durable selections and legacy isolation."""

from types import SimpleNamespace

import pytest

from ._loader import load_integration_module
from .test_channel_services import _setup
from .test_poll_channel import POLL_ID, _manager, _select

polls = load_integration_module("polls")
sending = load_integration_module("poll_send")


@pytest.mark.asyncio
async def test_application_selection_saved_before_fire_and_replayed_without_legacy():
    manager, bus = _manager(person_entity_id="person.seba")
    pending = manager._registry.pending(POLL_ID)
    pending.origin = "application"
    pending.client_request_id = "key-1"
    pending.emit_legacy_action = False
    _select(manager, "Confirm test")
    await manager._async_commit(POLL_ID, 123.0)
    assert len(bus.events) == 1
    event = bus.events[0][1]
    assert event["origin"] == "application"
    assert event["vote_occurred_at"] == "1970-01-01T00:02:03+00:00"
    assert event["poll"]["selected_option"] is None
    assert event["poll"]["action_id"] == "TEST_WAHA_V14"
    assert manager._store.saved[-1]["selections"][0]["payload"] == event
    restored = polls.PollRegistry()
    restored.load(manager._store.saved[-1])
    assert restored.selections("direct_opaque", 0, 100)["events"] == [event]
    assert restored.selections("other", 0, 100)["events"] == []
    assert (
        restored.selections("direct_opaque", event["selection_sequence"], 100)["events"]
        == []
    )
    await manager._async_commit(POLL_ID, 123.0)
    assert len(bus.events) == 1


@pytest.mark.asyncio
async def test_application_poll_key_replay_and_conflict(monkeypatch):
    env = await _setup(monkeypatch)
    registered = []

    async def register(*args, **kwargs):
        registered.append((args, kwargs))

    env.entry.runtime_data.poll_manager = SimpleNamespace(async_register_poll=register)
    kwargs = dict(
        conversation_id=env.direct,
        client_request_id="poll-1",
        message="Promemoria",
        actions=[
            {"action": "opaque1", "title": "Fatto"},
            {"action": "opaque2", "title": "15 min"},
        ],
    )
    result = await sending.async_send_poll_to_conversation(env.entry, **kwargs)
    replay = await sending.async_send_poll_to_conversation(env.entry, **kwargs)
    assert result["status"] == "accepted"
    assert replay["replayed"] is True
    assert len(registered) == 1
    assert registered[0][1] == dict(
        origin="application", client_request_id="poll-1", emit_legacy_action=False
    )
    assert len(registered[0][0][2]) == 2
    conflict = await sending.async_send_poll_to_conversation(
        env.entry, **{**kwargs, "message": "Changed"}
    )
    assert conflict["status"] == "failed"
    assert conflict["error_code"] == "request_conflict"
    assert len(registered) == 1


def test_journal_restore_rejects_invalid_sequences_and_pages():
    registry = polls.PollRegistry()
    registry.load(
        {
            "polls": [],
            "selections": [
                {
                    "payload": {
                        "origin": "application",
                        "type": "poll.selection_settled",
                        "selection_sequence": True,
                        "conversation_id": "direct",
                    },
                    "saved_at": float("nan"),
                }
            ],
        }
    )
    assert registry.selections("direct", 0, 100)["events"] == []
    for cursor, limit in [(True, 100), (0, 0), (0, 101), (-1, 10)]:
        with pytest.raises(polls.PollValidationError):
            registry.selections("direct", cursor, limit)


@pytest.mark.asyncio
async def test_failed_selection_write_never_publishes_and_keeps_pending_vote():
    manager, bus = _manager()
    pending = manager._registry.pending(POLL_ID)
    pending.origin = "application"
    pending.client_request_id = "app-request"
    pending.emit_legacy_action = False
    _select(manager, "Confirm test")

    async def unavailable(data):
        raise OSError("synthetic disk failure")

    manager._store.async_save = unavailable
    await manager._async_commit(POLL_ID, 123.0)
    assert not bus.events
    assert manager._registry.pending(POLL_ID) is pending
    assert not manager.selections("direct_opaque")["events"]
    assert manager.diagnostics["persistence_failures"] == 1
    assert manager.diagnostics["recovery_pending"] == 1
