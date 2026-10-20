"""Contract tests for the authenticated direct-chat inbound channel."""

import hashlib
import sys
from dataclasses import dataclass
from types import ModuleType, SimpleNamespace

import pytest

from ._loader import load_integration_module


class _Context:
    def __init__(self, *, user_id: str) -> None:
        self.user_id = user_id


# These contract tests run without the large Home Assistant installation.
_core_stub = ModuleType("homeassistant.core")
_core_stub.Context = _Context
_previous_core = sys.modules.get("homeassistant.core")
sys.modules["homeassistant.core"] = _core_stub
try:
    inbound = load_integration_module("inbound")
finally:
    if _previous_core is None:
        del sys.modules["homeassistant.core"]
    else:
        sys.modules["homeassistant.core"] = _previous_core


PHONE = "393331234567@c.us"
LID = "178563278901234@lid"
LID_DEVICE = "178563278901234:2@lid"
OTHER_PHONE = "393339999999@c.us"
NOW = 1_755_776_096


@pytest.fixture(autouse=True)
def _fixed_clock(monkeypatch):
    """Keep timestamp contract tests independent of the host's wall clock."""
    monkeypatch.setattr(inbound, "time", SimpleNamespace(time=lambda: NOW))


@dataclass
class _Contact:
    conversation_id: str = "direct_abc"
    subentry_id: str = "contact_abc"
    notify_entity_id: str = "notify.waha_seba"
    person_entity_id: str | None = "person.seba"

    def event_sender(self) -> dict[str, str]:
        sender = {"contact_id": self.subentry_id}
        if self.notify_entity_id:
            sender["notify_entity_id"] = self.notify_entity_id
        if self.person_entity_id:
            sender["person_entity_id"] = self.person_entity_id
        return sender


class _Registry:
    def __init__(self, contact: _Contact | None = None) -> None:
        self.contact = contact or _Contact()
        self.claimed: set[str] = set()
        self.messages: set[tuple[str, str]] = set()

    async def async_resolve_chat(self, chat_id: str) -> _Contact | None:
        if chat_id in (PHONE, LID, LID_DEVICE):
            return self.contact
        if chat_id == OTHER_PHONE:
            return _Contact(
                conversation_id="direct_other",
                subentry_id="contact_other",
                notify_entity_id="notify.waha_other",
                person_entity_id=None,
            )
        return None

    def message_token(self, raw_id: str) -> str:
        return "opaque_" + hashlib.sha256(raw_id.encode()).hexdigest()[:16]

    def event_token(self, raw_id: str) -> str:
        return "evt_" + hashlib.sha256(raw_id.encode()).hexdigest()[:16]

    async def async_remember_message(
        self, raw_id: str, conversation_id: str, **_provenance
    ) -> str:
        self.messages.add((raw_id, conversation_id))
        return self.message_token(raw_id)

    async def async_message_known(self, raw_id: str, conversation_id: str) -> bool:
        return (raw_id, conversation_id) in self.messages

    async def async_claim_event(self, raw_event_id: str) -> bool:
        if raw_event_id in self.claimed:
            return False
        self.claimed.add(raw_event_id)
        return True


class _Bus:
    def __init__(self) -> None:
        self.fired: list[tuple[str, dict, _Context | None]] = []

    def async_fire(self, event_type: str, data: dict, *, context=None) -> None:
        self.fired.append((event_type, data, context))


class _Auth:
    async def async_get_user(self, user_id: str):
        if user_id == "active-user":
            return SimpleNamespace(id=user_id, is_active=True)
        return None


def _setup(contact: _Contact | None = None):
    registry = _Registry(contact)
    bus = _Bus()
    hass = SimpleNamespace(
        bus=bus,
        states=SimpleNamespace(
            get=lambda entity_id: (
                SimpleNamespace(attributes={"user_id": "active-user"})
                if entity_id == "person.seba"
                else None
            )
        ),
        auth=_Auth(),
    )
    entry = SimpleNamespace(data={"session": "house"}, entry_id="entry_abc")
    return inbound.WahaInboundManager(hass, entry, registry), registry, bus


def _event(event="message", **payload_overrides):
    payload = {
        "id": "false_393331234567@c.us_ABCD",
        "timestamp": NOW,
        "from": PHONE,
        "fromMe": False,
        "to": "390001111111@c.us",
        "body": "ping",
        "hasMedia": False,
        **payload_overrides,
    }
    return {
        "id": "evt_ABC",
        "timestamp": NOW * 1000,
        "event": event,
        "session": "house",
        "payload": payload,
    }


@pytest.mark.asyncio
async def test_configured_contact_keeps_direct_route_with_current_membership_metadata():
    manager, _registry, bus = _setup()

    class GuestMembership:
        def resolve_member(self, alias, *, occurred_at):
            assert alias == PHONE
            assert occurred_at == NOW
            return {
                "participant_id": "participant_opaque",
                "membership_id": "membership_current",
                "kind": "host",
                "status": "active",
                "whatsapp_role": "admin",
            }

        def snapshot(self):
            return {"ready": True, "group_id": "group_opaque"}

    manager._guest_registry = GuestMembership()
    await manager.async_handle_payload(_event())
    assert len(bus.fired) == 1
    data = bus.fired[0][1]
    assert data["conversation_id"] == "direct_abc"
    assert data["sender"] == {
        "contact_id": "contact_abc",
        "notify_entity_id": "notify.waha_seba",
        "person_entity_id": "person.seba",
        "participant_id": "participant_opaque",
    }
    assert data["group"] == {"group_id": "group_opaque", "purpose": "guests"}
    assert data["membership"]["membership_id"] == "membership_current"


@pytest.mark.asyncio
async def test_configured_contact_omits_membership_when_refresh_fails():
    """A removed guest still has a contact event, without stale group claims."""
    manager, _registry, bus = _setup()

    class StaleGuest:
        def resolve_member(self, _alias, *, occurred_at):
            assert occurred_at == NOW
            return {"participant_id": "stale", "membership_id": "old"}

        def snapshot(self):
            return {"ready": True, "group_id": "group_opaque"}

    calls = []

    async def refresh():
        calls.append(True)
        return {"ready": False}

    manager._guest_registry = StaleGuest()
    manager._refresh_guest_membership = refresh
    await manager.async_handle_payload(_event())

    assert calls == [True]
    assert len(bus.fired) == 1
    data = bus.fired[0][1]
    assert data["conversation_type"] == "direct"
    assert data["sender"]["contact_id"] == "contact_abc"
    assert "participant_id" not in data["sender"]
    assert "membership" not in data
    assert "group" not in data


@pytest.mark.asyncio
async def test_unprompted_text_event_has_stable_opaque_channel_and_person_context():
    manager, registry, bus = _setup()

    await manager.async_handle_payload(_event())

    assert len(bus.fired) == 1
    event_type, data, context = bus.fired[0]
    assert event_type == "waha_whatsapp_event"
    assert data == {
        "schema_version": 1,
        "event_id": registry.event_token("evt_ABC"),
        "type": "message.received",
        "config_entry_id": "entry_abc",
        "conversation_id": "direct_abc",
        "conversation_type": "direct",
        "occurred_at": "2025-08-21T11:34:56+00:00",
        "sender": {
            "contact_id": "contact_abc",
            "notify_entity_id": "notify.waha_seba",
            "person_entity_id": "person.seba",
        },
        "message": {
            "id": registry.message_token("false_393331234567@c.us_ABCD"),
            "in_reply_to": None,
            "kind": "text",
            "text": "ping",
        },
    }
    assert context.user_id == "active-user"
    assert PHONE not in str(data)
    assert "false_393331234567" not in str(data)


@pytest.mark.asyncio
async def test_lid_sender_is_resolved_and_optional_person_is_absent():
    manager, _, bus = _setup(_Contact(person_entity_id=None))

    await manager.async_handle_payload(_event(**{"from": LID}))

    assert len(bus.fired) == 1
    _, data, context = bus.fired[0]
    assert data["sender"] == {
        "contact_id": "contact_abc",
        "notify_entity_id": "notify.waha_seba",
    }
    assert context is None
    assert LID not in str(data)


@pytest.mark.asyncio
async def test_phone_and_lid_chat_fields_may_represent_the_same_direct_contact():
    manager, _, bus = _setup()
    await manager.async_handle_payload(
        _event(**{"from": LID_DEVICE, "chatId": PHONE, "participant": LID})
    )

    assert len(bus.fired) == 1
    assert bus.fired[0][1]["conversation_id"] == "direct_abc"
    assert LID_DEVICE not in str(bus.fired[0][1])


@pytest.mark.asyncio
async def test_gows_group_with_configured_participant_is_not_a_direct_message():
    manager, _, bus = _setup()
    event = _event(
        **{
            "from": PHONE,
            "chatId": "120363000000000000@g.us",
            "participant": PHONE,
        }
    )
    event["engine"] = "GOWS"

    await manager.async_handle_payload(event)

    assert bus.fired == []
    assert manager.rejection_reasons == {"not_direct_chat": 1}


@pytest.mark.asyncio
@pytest.mark.parametrize("field", ["chatId", "participant"])
async def test_other_configured_person_cannot_be_correlated_to_sender(field):
    manager, _, bus = _setup()

    await manager.async_handle_payload(_event(**{field: OTHER_PHONE}))

    assert bus.fired == []
    assert manager.rejection_reasons == {"chat_identity_mismatch": 1}


@pytest.mark.asyncio
async def test_reply_reference_is_opaque_even_when_original_was_not_tracked():
    manager, registry, bus = _setup()
    await manager.async_handle_payload(
        _event(replyTo={"id": "true_393331234567@c.us_OLD", "body": "secret"})
    )

    assert bus.fired[0][1]["message"]["in_reply_to"] == registry.message_token(
        "true_393331234567@c.us_OLD"
    )
    assert "secret" not in str(bus.fired[0][1])


@pytest.mark.asyncio
async def test_media_placeholder_excludes_url_binary_and_separates_caption():
    manager, _, bus = _setup()
    await manager.async_handle_payload(
        _event(
            body="Look at this",
            hasMedia=True,
            media={
                "url": "http://waha.local/api/files/private-audio.ogg",
                "mimetype": "audio/ogg",
                "filename": "secret.ogg",
            },
        )
    )

    message = bus.fired[0][1]["message"]
    assert message["kind"] == "audio"
    assert message["mime_type"] == "audio/ogg"
    assert message["caption"] == "Look at this"
    assert "url" not in str(message)
    assert "secret.ogg" not in str(message)


@pytest.mark.asyncio
async def test_media_without_caption_and_mime_parameters_is_still_a_placeholder():
    manager, _, bus = _setup()
    await manager.async_handle_payload(
        _event(
            hasMedia=True,
            media={"mimetype": "Audio/Ogg; codecs=opus", "url": None},
            body=None,
        )
    )

    message = bus.fired[0][1]["message"]
    assert message["kind"] == "audio"
    assert message["mime_type"] == "audio/ogg"
    assert "caption" not in message


@pytest.mark.asyncio
async def test_reaction_add_remove_and_target_known_are_structured_only():
    manager, registry, bus = _setup()
    registry.messages.add(("outbound-1", "direct_abc"))
    added = _event(
        "message.reaction",
        reaction={"text": "👍", "messageId": "outbound-1"},
    )
    removed = _event(
        "message.reaction",
        reaction={"text": "", "messageId": "outbound-1"},
    )
    removed["id"] = "evt_REMOVED"
    await manager.async_handle_payload(added)
    await manager.async_handle_payload(removed)

    assert [item[1]["type"] for item in bus.fired] == [
        "reaction.added",
        "reaction.removed",
    ]
    assert bus.fired[0][1]["reaction"] == {
        "emoji": "👍",
        "target_message_id": registry.message_token("outbound-1"),
        "target_known": True,
    }
    assert bus.fired[1][1]["reaction"]["emoji"] == ""
    assert all("action" not in item[1] for item in bus.fired)


@pytest.mark.asyncio
async def test_unknown_reaction_target_is_reported_but_not_trusted():
    manager, _, bus = _setup()
    await manager.async_handle_payload(
        _event(
            "message.reaction",
            reaction={"text": "❤️", "messageId": "untracked"},
        )
    )

    assert bus.fired[0][1]["reaction"]["target_known"] is False


@pytest.mark.asyncio
async def test_duplicate_retry_is_suppressed():
    manager, _, bus = _setup()
    payload = _event()

    await manager.async_handle_payload(payload)
    await manager.async_handle_payload(payload)

    assert len(bus.fired) == 1
    assert manager.rejection_reasons == {"duplicate": 1}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("timestamp", "reason", "event_kind"),
    [
        (NOW - 3601, "stale_event", "message"),
        (NOW + 301, "future_event", "message"),
        (NOW - 3601, "stale_event", "message.reaction"),
        (NOW + 301, "future_event", "message.reaction"),
    ],
)
async def test_reconnect_history_or_future_timestamp_is_not_published(
    timestamp, reason, event_kind
):
    manager, _, bus = _setup()
    callback = _event(
        event_kind,
        timestamp=timestamp,
        **(
            {"reaction": {"text": "👍", "messageId": "outbound-1"}}
            if event_kind == "message.reaction"
            else {}
        ),
    )
    # A freshly delivered webhook must not mask the old message timestamp.
    callback["timestamp"] = NOW * 1000

    await manager.async_handle_payload(callback)

    assert bus.fired == []
    assert manager.rejection_reasons == {reason: 1}


@pytest.mark.asyncio
async def test_freshness_window_boundaries_are_accepted():
    manager, _, bus = _setup()
    old_edge = _event(timestamp=NOW - 3600)
    future_edge = _event(timestamp=NOW + 300)
    future_edge["id"] = "evt_FUTURE_EDGE"

    await manager.async_handle_payload(old_edge)
    await manager.async_handle_payload(future_edge)

    assert len(bus.fired) == 2


@pytest.mark.asyncio
async def test_legacy_event_without_envelope_id_uses_stable_fallback():
    manager, _, bus = _setup()
    payload = _event()
    del payload["id"]

    await manager.async_handle_payload(payload)
    await manager.async_handle_payload(payload)

    assert len(bus.fired) == 1
    assert bus.fired[0][1]["event_id"].startswith("evt_")


@pytest.mark.asyncio
async def test_legacy_reaction_fallback_includes_target():
    manager, _, bus = _setup()
    first = _event(
        "message.reaction", reaction={"text": "👍", "messageId": "outbound-1"}
    )
    second = _event(
        "message.reaction", reaction={"text": "👍", "messageId": "outbound-2"}
    )
    del first["id"]
    del second["id"]

    await manager.async_handle_payload(first)
    await manager.async_handle_payload(second)

    assert len(bus.fired) == 2
    assert bus.fired[0][1]["event_id"] != bus.fired[1][1]["event_id"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "change",
    [
        {"session": "another"},
        {"event": "message.any"},
        {"payload": {"fromMe": True}},
        {"payload": {"fromMe": None}},
        {"payload": {"from": "unknown@c.us"}},
        {"payload": {"from": "12345@g.us"}},
        {"payload": {"from": "status@broadcast"}},
        {"payload": {"to": "12345@g.us"}},
        {"payload": {"body": 123}},
        {"payload": {"body": "x" * 8193}},
        {"payload": {"id": ""}},
        {"payload": {"timestamp": "bad"}},
        {"payload": {"timestamp": "bad"}, "timestamp": "bad"},
        {"payload": {"hasMedia": "false"}},
        {"payload": {"replyTo": {"id": 123}}},
        {"payload": {"to": 42}},
        {"payload": {"chatId": "status@broadcast"}},
        {"payload": {"chatId": 42}},
        {"payload": {"participant": "12345@g.us"}},
        {
            "payload": {"reaction": {"text": "👍", "messageId": None}},
            "event": "message.reaction",
        },
    ],
)
async def test_invalid_or_untrusted_webhooks_fail_closed(change):
    manager, _, bus = _setup()
    event = _event()
    for key, value in change.items():
        if key == "payload":
            event["payload"].update(value)
        else:
            event[key] = value

    await manager.async_handle_payload(event)

    assert bus.fired == []
    assert manager.rejected_event_count == 1
