"""Managed guest traffic stays scoped to the confirmed group and membership."""

from copy import deepcopy
from time import time
from types import SimpleNamespace

import pytest

pytest.importorskip("homeassistant")

from ._loader import load_integration_module  # noqa: E402

guest_registry = load_integration_module("guest_registry")
guest_inbound = load_integration_module("guest_inbound")
commands = load_integration_module("commands")

GROUP = "120363123456789-1234567890@g.us"
BOT = "393331111111@c.us"
GUEST = "441234567890@c.us"
LID = "123456789@lid"


class FakeStore:
    def __init__(self):
        self.data = None

    async def async_load(self):
        return deepcopy(self.data)

    async def async_save(self, data):
        self.data = deepcopy(data)


class FakeChannel:
    """Only the channel contracts consumed by guest inbound processing."""

    def __init__(self):
        self.configured: dict[str, FakeContact] = {}
        self.events: set[str] = set()
        self.messages: list[tuple[str, str]] = []

    async def async_resolve_chat(self, jid):
        return self.configured.get(jid)

    def message_token(self, raw):
        return f"msg_{raw}"

    def event_token(self, raw):
        return f"evt_{raw}"

    async def async_remember_message(self, raw, conversation_id, **_provenance):
        self.messages.append((raw, conversation_id))
        return self.message_token(raw)

    async def async_message_known(self, raw, conversation_id):
        return (raw, conversation_id) in self.messages

    async def async_claim_event(self, raw):
        if raw in self.events:
            return False
        self.events.add(raw)
        return True


class FakeContact:
    def __init__(self, recipient):
        self.recipient = recipient

    def event_sender(self):
        return {
            "contact_id": "contact-1",
            "notify_entity_id": "notify.host",
            "person_entity_id": "person.host",
        }


async def make_manager(*, aliases=(GUEST,), configured=(), command_registry=None):
    """Start a ready private registry with one confirmed guest stay."""
    started = time() - 300
    registry = guest_registry.GuestRegistry(None, "entry-a", store=FakeStore())
    await registry.async_initialize_empty()
    await registry.async_start()
    await registry.async_bind_account(BOT)
    await registry.async_set_group(GROUP)
    await registry.async_set_provisioning_status("ready")
    await registry.async_reconcile(
        [
            {
                "aliases": list(aliases),
                "aliases_verified": len(aliases) > 1,
                "kind": "guest",
                "role": "participant",
            }
        ],
        observed_at=started,
        verified_bot_jid=BOT,
        security_verified=True,
        bot_admin_verified=True,
    )
    channel = FakeChannel()
    channel.configured.update(
        {jid: FakeContact(jid.split("@", 1)[0]) for jid in configured}
    )
    events = []
    hass = SimpleNamespace(
        bus=SimpleNamespace(
            async_fire=lambda event_type, data, **_kw: events.append((event_type, data))
        )
    )
    entry = SimpleNamespace(entry_id="entry-a", data={"session": "house"})
    manager = guest_inbound.WahaGuestInboundManager(
        hass,
        entry,
        registry,
        channel,
        verified_bot_jid=BOT,
        command_registry=command_registry,
    )
    return manager, registry, channel, events, started


@pytest.mark.asyncio
async def test_guest_command_requires_private_current_member_message():
    """The group itself cannot invoke the explicit private-only command."""
    events = []

    def fire(event_type, data, **_kw):
        events.append((event_type, data))
        if data["type"] == "message.received" and data["conversation_type"] == "direct":
            data["membership"]["status"] = "left"
            data["message"]["text"] = "/other"

    bus = SimpleNamespace(async_fire=fire)
    declarations = commands.CommandRegistry(
        SimpleNamespace(bus=bus), "entry-a", store=FakeStore()
    )
    await declarations.async_start()
    await declarations.async_register("status", [], [], True)
    manager, _registry, _channel, inbound_events, _ = await make_manager(
        command_registry=declarations
    )
    # Keep both publishers on one bus for ordering assertions.
    manager._hass.bus = bus
    group_message = envelope()
    group_message["payload"]["body"] = "/status"
    assert await manager.async_handle_payload(group_message) is True
    assert [data["type"] for _, data in events] == ["message.received"]

    private_message = envelope(sender=GUEST, participant=None)
    private_message["id"] = "WA-EVENT-2"
    private_message["payload"]["id"] = "WA-MESSAGE-2"
    private_message["payload"]["body"] = "/status"
    private_message["payload"]["chatId"] = GUEST
    assert await manager.async_handle_payload(private_message) is True
    assert [data["type"] for _, data in events] == [
        "message.received",
        "message.received",
        "command.requested",
    ]
    assert events[-1][1]["membership"]["status"] == "active"
    assert inbound_events == []


def envelope(*, sender=GROUP, participant=GUEST, timestamp=None, event="message"):
    payload = {
        "from": sender,
        "fromMe": False,
        "id": "WA-MESSAGE-1",
        "timestamp": time() if timestamp is None else timestamp,
        "body": "Hello",
        "hasMedia": False,
    }
    if participant is not None:
        payload["participant"] = participant
    return {"event": event, "id": "WA-EVENT-1", "session": "house", "payload": payload}


@pytest.mark.asyncio
async def test_group_message_uses_participant_not_group_as_sender():
    manager, registry, channel, events, _started = await make_manager()

    accepted = await manager.async_handle_payload(envelope())

    assert accepted is True
    assert len(events) == 1
    event_type, data = events[0]
    assert event_type == "waha_whatsapp_event"
    assert data["schema_version"] == 1
    assert data["conversation_type"] == "group"
    assert data["conversation_id"] == registry.snapshot()["conversation_id"]
    assert data["group"] == {
        "group_id": registry.snapshot()["group_id"],
        "purpose": "guests",
    }
    assert data["sender"]["participant_id"].startswith("participant_")
    assert data["membership"]["membership_id"].startswith("membership_")
    assert (
        data["membership"]["direct_conversation_id"]
        == (data["sender"]["direct_conversation_id"])
    )
    assert data["message"] == {
        "kind": "text",
        "text": "Hello",
        "in_reply_to": None,
        "id": "msg_WA-MESSAGE-1",
    }
    assert channel.messages == [("WA-MESSAGE-1", data["conversation_id"])]
    assert GROUP not in str(data)
    assert GUEST not in str(data)


@pytest.mark.asyncio
async def test_group_chat_id_with_participant_from_is_not_lost():
    """GOWS can put the sender in from and the group in chatId."""
    manager, registry, _channel, events, _started = await make_manager()
    data = envelope(sender=GUEST, participant=GUEST)
    data["payload"]["chatId"] = GROUP

    assert await manager.async_handle_payload(data) is True
    assert events[0][1]["conversation_type"] == "group"
    assert events[0][1]["conversation_id"] == registry.snapshot()["conversation_id"]


@pytest.mark.asyncio
async def test_group_chat_id_requires_same_confirmed_sender():
    manager, _registry, _channel, events, _started = await make_manager()
    data = envelope(sender="999999@c.us", participant=GUEST)
    data["payload"]["chatId"] = GROUP

    assert await manager.async_handle_payload(data) is False
    assert events == []


@pytest.mark.asyncio
async def test_unconfigured_member_direct_message_gets_stay_route():
    manager, registry, _channel, events, _started = await make_manager()
    data = envelope(sender=GUEST, participant=None)
    data["payload"]["chatId"] = GUEST

    assert await manager.async_handle_payload(data) is True

    event = events[0][1]
    member = registry.snapshot()["memberships"][0]
    assert event["conversation_type"] == "direct"
    assert event["conversation_id"] == member["direct_conversation_id"]
    assert event["sender"]["participant_id"] == member["participant_id"]
    assert GUEST not in str(event)


@pytest.mark.asyncio
async def test_configured_contact_private_message_is_left_to_existing_handler():
    manager, _registry, _channel, events, _started = await make_manager(
        configured=[GUEST]
    )

    assert (
        await manager.async_handle_payload(envelope(sender=GUEST, participant=None))
        is False
    )
    assert events == []


@pytest.mark.asyncio
async def test_group_sender_gets_contact_metadata_only_for_same_member():
    manager, _registry, channel, events, _started = await make_manager(
        configured=[GUEST]
    )

    assert await manager.async_handle_payload(envelope()) is True
    assert events[0][1]["sender"]["contact_id"] == "contact-1"
    assert events[0][1]["sender"]["notify_entity_id"] == "notify.host"
    assert events[0][1]["sender"]["person_entity_id"] == "person.host"
    assert len(events) == 1

    # A resolver result for a different configured PN is not identity proof.
    manager, _registry, channel, events, _started = await make_manager()
    channel.configured[GUEST] = FakeContact("393339999999")
    assert await manager.async_handle_payload(envelope()) is True
    assert "contact_id" not in events[0][1]["sender"]


@pytest.mark.asyncio
async def test_non_string_direct_destination_is_rejected():
    manager, _registry, _channel, events, _started = await make_manager()
    data = envelope(sender=GUEST, participant=None)
    data["payload"]["to"] = {"id": BOT}

    assert await manager.async_handle_payload(data) is False
    assert events == []


@pytest.mark.asyncio
async def test_unrelated_group_unknown_guest_and_wrong_session_are_ignored():
    manager, _registry, _channel, events, _started = await make_manager()
    other_group = envelope(sender="98765@g.us")
    unknown = envelope(participant="999999999@c.us")
    wrong_session = envelope()
    wrong_session["session"] = "other"

    assert await manager.async_handle_payload(other_group) is False
    assert await manager.async_handle_payload(unknown) is False
    assert await manager.async_handle_payload(wrong_session) is False
    assert events == []


@pytest.mark.asyncio
async def test_old_or_untrustworthy_timestamp_never_gets_current_stay():
    manager, _registry, _channel, events, started = await make_manager()
    before_join = envelope(timestamp=started - 1)
    missing = envelope()
    del missing["payload"]["timestamp"]
    missing["timestamp"] = time()
    stale = envelope(timestamp=time() - 4000)

    assert await manager.async_handle_payload(before_join) is False
    assert await manager.async_handle_payload(missing) is False
    assert await manager.async_handle_payload(stale) is False
    assert events == []


@pytest.mark.asyncio
async def test_verified_lid_aliases_must_refer_to_same_member():
    manager, _registry, _channel, events, _started = await make_manager(
        aliases=(GUEST, LID)
    )
    valid = envelope(sender=GUEST, participant=LID)
    valid["payload"]["chatId"] = LID
    invalid = envelope(sender=GUEST, participant="999999@lid")

    assert await manager.async_handle_payload(valid) is True
    assert await manager.async_handle_payload(invalid) is False
    assert len(events) == 1


@pytest.mark.asyncio
async def test_reaction_uses_same_membership_and_deduplicates():
    manager, _registry, _channel, events, _started = await make_manager()
    reaction = envelope(event="message.reaction")
    reaction["payload"]["reaction"] = {
        "text": "👍",
        "messageId": "TARGET-1",
    }

    assert await manager.async_handle_payload(reaction) is True
    assert await manager.async_handle_payload(reaction) is False
    assert events[0][1]["type"] == "reaction.added"
    assert events[0][1]["reaction"] == {
        "emoji": "👍",
        "target_message_id": "msg_TARGET-1",
        "target_known": False,
    }


@pytest.mark.asyncio
async def test_changed_bot_account_suspends_publication():
    manager, _registry, _channel, events, _started = await make_manager()
    manager.update_verified_bot_jid("393332222222@c.us")

    assert await manager.async_handle_payload(envelope()) is False
    assert events == []


@pytest.mark.asyncio
async def test_inbound_requires_fresh_roster_and_account_confirmation():
    """A cached membership cannot publish after the live check fails."""
    manager, registry, _channel, events, _started = await make_manager()
    checks = []

    async def unavailable():
        checks.append(True)
        registry.suspend()
        return registry.snapshot()

    manager._refresh_membership = unavailable

    assert await manager.async_handle_payload(envelope()) is False
    assert checks == [True]
    assert events == []


@pytest.mark.parametrize("invalid_id", [None, 42, [], {}])
async def test_malformed_incoming_id_remains_a_safe_refusal(invalid_id):
    manager, _registry, _channel, events, _started = await make_manager()
    data = envelope()
    data["payload"]["id"] = invalid_id
    assert await manager.async_handle_payload(data) is False
    assert events == []


async def test_guest_leave_rejoin_during_contact_lookup_keeps_old_read_provenance(
    monkeypatch,
):
    """An old group message must not acquire a new guest stay's read grant."""
    from unittest.mock import AsyncMock

    from .test_activity import Clock

    activity = load_integration_module("activity")
    channel_module = load_integration_module("channel_registry")
    monkeypatch.setattr(channel_module, "_notify_entity_id", lambda *_args: None)
    manager, guests, _old_channel, events, _started = await make_manager()
    entry = manager._entry
    entry.state = "loaded"
    entry.subentries = {}
    entry.data["webhook_secret"] = "synthetic-secret"
    client = SimpleNamespace(
        session_name="house",
        async_get_server=AsyncMock(
            return_value=SimpleNamespace(engine="GOWS", version="2026.9.1")
        ),
        async_get_session=AsyncMock(
            return_value=SimpleNamespace(name="house", status="WORKING", account_id=BOT)
        ),
        async_mark_seen=AsyncMock(),
    )
    channel = channel_module.ChannelRegistry(
        manager._hass, entry, client, store=FakeStore()
    )
    channel.set_extra_route_resolver(
        lambda conv: (
            guests.resolve_route(conv) or guests.resolve_group_conversation(conv)
        )
    )
    await channel.async_start()
    entry.runtime_data = SimpleNamespace(
        session=SimpleNamespace(account_id=BOT),
        client=client,
        channel_registry=channel,
        general_groups=SimpleNamespace(resolve_conversation=lambda _conv: None),
        guest_registry=guests,
        guest_manager=SimpleNamespace(
            async_reconcile=AsyncMock(return_value={"ready": True}),
            async_verify_group_destination=AsyncMock(return_value=True),
            async_verify_guest_destination=AsyncMock(return_value=True),
        ),
    )
    activity_manager = activity.ActivityManager(
        manager._hass, entry, schedule=Clock().schedule
    )
    entry.runtime_data.activity_manager = activity_manager
    manager._channel = channel
    previous = guests.resolve_member(GUEST, occurred_at=time())

    async def leave_then_rejoin(_jid):
        await guests.async_reconcile(
            [],
            observed_at=time() - 2,
            verified_bot_jid=BOT,
            security_verified=True,
            bot_admin_verified=True,
        )
        await guests.async_reconcile(
            [{"aliases": [GUEST], "kind": "guest", "role": "participant"}],
            observed_at=time() - 1,
            verified_bot_jid=BOT,
            security_verified=True,
            bot_admin_verified=True,
        )
        return None

    channel.async_resolve_chat = leave_then_rejoin
    data = envelope()
    data["payload"]["id"] = f"false_{GROUP}_EXAMPLE01_{GUEST}"
    data["payload"]["timestamp"] = time() - 3
    assert await manager.async_handle_payload(data) is True
    message = events[-1][1]
    assert message["membership"]["membership_id"] == previous["membership_id"]
    assert (
        guests.resolve_member(GUEST, occurred_at=time())["membership_id"]
        != previous["membership_id"]
    )
    response = await activity_manager.async_mark_read(
        {
            "config_entry_id": entry.entry_id,
            "conversation_id": message["conversation_id"],
            "message_id": message["message"]["id"],
        },
        AsyncMock(),
    )
    assert response["error_code"] == "invalid_message"
    client.async_mark_seen.assert_not_awaited()
