"""Settled poll selections use the channel event without changing legacy actions."""

import sys
from types import ModuleType, SimpleNamespace

import pytest

from ._loader import load_integration_module


class _Context:
    def __init__(self, *, user_id: str) -> None:
        self.user_id = user_id


_stubs = {
    "homeassistant.config_entries": {"ConfigEntry": object},
    "homeassistant.core": {"Context": _Context, "HomeAssistant": object},
    "homeassistant.helpers.storage": {"Store": object},
}
_previous = {name: sys.modules.get(name) for name in _stubs}
for _name, _attributes in _stubs.items():
    _module = ModuleType(_name)
    for _attribute, _value in _attributes.items():
        setattr(_module, _attribute, _value)
    sys.modules[_name] = _module
try:
    manager_module = load_integration_module("poll_manager")
finally:
    for _name, _module in _previous.items():
        if _module is None:
            del sys.modules[_name]
        else:
            sys.modules[_name] = _module

polls = load_integration_module("polls")
PHONE = "393331234567@c.us"
POLL_ID = "true_393331234567@c.us_ABCD1234"


class _Bus:
    def __init__(self) -> None:
        self.events = []

    def async_fire(self, event_type, data, *, context=None) -> None:
        self.events.append((event_type, data, context))


class _Store:
    def __init__(self) -> None:
        self.saved = []

    async def async_save(self, data) -> None:
        self.saved.append(data)

    async def async_load(self):
        return self.saved[-1] if self.saved else None


class _Loop:
    def __init__(self) -> None:
        self.scheduled = []

    def call_later(self, delay, callback, *args):
        timer = SimpleNamespace(cancel=lambda: None)
        self.scheduled.append((delay, callback, args))
        return timer


class _Auth:
    async def async_get_user(self, user_id):
        return SimpleNamespace(id=user_id, is_active=True)


class _Contact:
    conversation_id = "direct_opaque"

    def __init__(self, person_entity_id=None) -> None:
        self.person_entity_id = person_entity_id

    def event_sender(self):
        sender = {
            "contact_id": "contact_opaque",
            "notify_entity_id": "notify.waha_seba",
        }
        if self.person_entity_id is not None:
            sender["person_entity_id"] = self.person_entity_id
        return sender


class _ChannelRegistry:
    def __init__(self, person_entity_id=None) -> None:
        self.contact = _Contact(person_entity_id)

    async def async_resolve_chat(self, chat_id):
        return self.contact if chat_id == PHONE else None

    def resolve_conversation(self, conversation_id):
        if conversation_id == "direct_opaque":
            return self.contact
        return None

    def event_token(self, raw_id):
        return "evt_opaque"

    def message_token(self, raw_id):
        return "msg_opaque"


def _manager(*, person_entity_id=None, conversation_id="direct_opaque"):
    bus = _Bus()
    hass = SimpleNamespace(
        data={},
        bus=bus,
        loop=_Loop(),
        states=SimpleNamespace(
            get=lambda entity_id: (
                SimpleNamespace(attributes={"user_id": "user-seba"})
                if entity_id == "person.seba"
                else None
            )
        ),
        auth=_Auth(),
    )
    manager = manager_module.WahaPollManager(
        hass,
        SimpleNamespace(entry_id="entry_opaque"),
        SimpleNamespace(session_name="default"),
        "synthetic-signing-key",
        _ChannelRegistry(person_entity_id),
        store=_Store(),
    )
    # This synchronous unit fixture represents an initialized active runtime;
    # end-to-end fixtures additionally exercise async_start's actual store load.
    manager._writer_slot.owner = manager
    options = polls.build_poll_options(
        [{"action": "TEST_WAHA_V14", "title": "Confirm test"}], "No action"
    )
    manager._registry.register(
        POLL_ID,
        PHONE,
        options,
        0,
        person_entity_id,
        conversation_id,
    )
    return manager, bus


def _select(manager, title):
    vote = polls.PollVote(
        POLL_ID,
        PHONE,
        PHONE,
        title,
        123.0,
    )
    assert manager._registry.apply_vote_with_reason(vote, received_at=0) is None


@pytest.mark.asyncio
async def test_settled_action_emits_both_events_with_same_person_context() -> None:
    manager, bus = _manager(person_entity_id="person.seba")
    _select(manager, "Confirm test")

    await manager._async_commit(POLL_ID, 123.0)
    await manager._async_commit(POLL_ID, 123.0)

    assert len(bus.events) == 2
    legacy_type, legacy_data, legacy_context = bus.events[0]
    assert legacy_type == "mobile_app_notification_action"
    assert legacy_data == {"action": "TEST_WAHA_V14"}
    channel_type, channel_data, channel_context = bus.events[1]
    assert channel_type == "waha_whatsapp_event"
    assert channel_data["type"] == "poll.selection_settled"
    assert channel_data["schema_version"] == 1
    assert channel_data["event_id"] == "evt_opaque"
    assert channel_data["config_entry_id"] == "entry_opaque"
    assert channel_data["conversation_id"] == "direct_opaque"
    assert channel_data["conversation_type"] == "direct"
    assert channel_data["sender"] == _Contact("person.seba").event_sender()
    assert channel_data["poll"] == {
        "message_id": "msg_opaque",
        "selected_option": "Confirm test",
        "action_id": "TEST_WAHA_V14",
    }
    assert legacy_context is channel_context
    assert channel_context.user_id == "user-seba"
    assert PHONE not in str(channel_data)
    assert POLL_ID not in str(channel_data)


@pytest.mark.asyncio
async def test_no_action_selection_emits_only_channel_event() -> None:
    manager, bus = _manager()
    _select(manager, "No action")

    await manager._async_commit(POLL_ID, 123.0)

    assert len(bus.events) == 1
    event_type, data, context = bus.events[0]
    assert event_type == "waha_whatsapp_event"
    assert data["poll"]["selected_option"] == "No action"
    assert data["poll"]["action_id"] is None
    assert "person_entity_id" not in data["sender"]
    assert context is None


@pytest.mark.asyncio
async def test_quick_correction_does_not_publish_superseded_action() -> None:
    manager, bus = _manager()
    _select(manager, "Confirm test")
    correction = polls.PollVote(POLL_ID, PHONE, PHONE, "No action", 124.0)
    assert manager._registry.apply_vote_with_reason(correction, received_at=0) is None

    await manager._async_commit(POLL_ID, 123.0)
    assert not bus.events
    await manager._async_commit(POLL_ID, 124.0)

    assert len(bus.events) == 1
    assert bus.events[0][1]["poll"]["selected_option"] == "No action"
    assert bus.events[0][1]["poll"]["action_id"] is None


@pytest.mark.asyncio
async def test_old_or_changed_contact_poll_preserves_legacy_without_channel_event():
    for conversation_id in (None, "direct_old_contact"):
        manager, bus = _manager(conversation_id=conversation_id)
        _select(manager, "Confirm test")

        await manager._async_commit(POLL_ID, 123.0)

        assert len(bus.events) == 1
        assert bus.events[0][0] == "mobile_app_notification_action"


@pytest.mark.asyncio
async def test_poll_registration_remembers_safe_conversation_handle() -> None:
    manager, _ = _manager()
    options = polls.build_poll_options(
        [{"action": "SECOND", "title": "Second"}], "No action"
    )

    await manager.async_register_poll("poll-two", PHONE, options, 5)

    assert manager._registry.pending("poll-two").conversation_id == "direct_opaque"
    assert manager._store.saved[-1]["polls"][-1]["conversation_id"] == "direct_opaque"


@pytest.mark.asyncio
async def test_authenticated_vote_path_settles_into_both_events() -> None:
    manager, bus = _manager()
    payload = {
        "event": "poll.vote",
        "session": "default",
        "payload": {
            "poll": {"id": POLL_ID, "fromMe": True, "to": PHONE},
            "vote": {
                "fromMe": False,
                "from": PHONE,
                "timestamp": 123.0,
                "selectedOptions": ["Confirm test"],
            },
        },
    }

    await manager.async_handle_payload(payload)
    assert len(manager._hass.loop.scheduled) == 1
    assert not bus.events
    await manager._async_commit(POLL_ID, 123.0)

    assert [event[0] for event in bus.events] == [
        "mobile_app_notification_action",
        "waha_whatsapp_event",
    ]


@pytest.mark.asyncio
async def test_unknown_poll_vote_does_not_publish_channel_event() -> None:
    manager, bus = _manager()
    payload = {
        "event": "poll.vote",
        "session": "default",
        "payload": {
            "poll": {"id": "not-registered", "fromMe": True, "to": PHONE},
            "vote": {
                "fromMe": False,
                "from": PHONE,
                "timestamp": 123.0,
                "selectedOptions": ["Confirm test"],
            },
        },
    }

    await manager.async_handle_payload(payload)

    assert not bus.events
    assert manager.rejected_vote_reasons == {"unknown_poll": 1}
