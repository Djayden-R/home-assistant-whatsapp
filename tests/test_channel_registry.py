"""Bounded private routing and correlation for the bidirectional channel."""

from __future__ import annotations

from copy import deepcopy
from types import SimpleNamespace

import pytest

from ._loader import load_integration_module

registry_module = load_integration_module("channel_registry")


class FakeStore:
    """Round-trip Home Assistant's private store API without disk I/O."""

    def __init__(self, data=None) -> None:
        self.data = deepcopy(data)
        self.save_count = 0
        self.fail_loads = 0
        self.fail_saves = 0

    async def async_load(self):
        if self.fail_loads:
            self.fail_loads -= 1
            raise OSError("Storage unavailable")
        return deepcopy(self.data)

    async def async_save(self, data) -> None:
        if self.fail_saves:
            self.fail_saves -= 1
            raise OSError("Storage unavailable")
        self.data = deepcopy(data)
        self.save_count += 1


class FakeClient:
    """Resolve only explicitly configured LID mappings."""

    session_name = "house"

    def __init__(self, mappings=None) -> None:
        self.mappings = mappings or {}
        self.lookups = []

    async def async_resolve_lid(self, lid):
        self.lookups.append(lid)
        return self.mappings.get(lid)


def make_entry(*, entry_id="entry-a", secret="one-private-secret", subentries=None):
    contacts = subentries or {
        "sub-a": SimpleNamespace(
            subentry_id="sub-a",
            subentry_type="recipient",
            title="Seba",
            data={"recipient": "393331234567", "person_entity_id": "person.seba"},
        ),
        "sub-b": SimpleNamespace(
            subentry_id="sub-b",
            subentry_type="recipient",
            title="Guest",
            data={"recipient": "441234567890"},
        ),
    }
    return SimpleNamespace(
        entry_id=entry_id,
        data={"webhook_secret": secret},
        subentries=contacts,
    )


def make_registry(monkeypatch, *, entry=None, store=None, client=None):
    monkeypatch.setattr(
        registry_module,
        "_notify_entity_id",
        lambda _hass, _entry_id, subentry_id: f"notify.waha_{subentry_id}",
    )
    return registry_module.ChannelRegistry(
        SimpleNamespace(),
        entry or make_entry(),
        client or FakeClient(),
        store=store or FakeStore(),
    )


@pytest.mark.asyncio
async def test_external_group_route_tracks_quotes_only_while_confirmed(monkeypatch):
    registry = make_registry(monkeypatch)
    await registry.async_start()
    route = "group_conversation_opaque"
    enabled = True

    def resolver(conversation_id):
        return "120363123456789@g.us" if enabled and conversation_id == route else None

    registry.set_extra_route_resolver(resolver)
    token = await registry.async_remember_message("outbound-id", route)
    assert registry.resolve_message(token, route) == "outbound-id"
    assert registry.resolve_message(token, "other") is None
    enabled = False
    assert registry.resolve_message(token, route) is None


@pytest.mark.asyncio
async def test_guest_route_message_cannot_follow_new_membership(monkeypatch):
    registry = make_registry(monkeypatch)
    await registry.async_start()
    destinations = {"guest_direct_stay_a": "441234567890@c.us"}
    registry.set_extra_route_resolver(destinations.get)
    token = await registry.async_remember_message(
        "guest-message", "guest_direct_stay_a"
    )
    assert registry.resolve_message(token, "guest_direct_stay_a") == "guest-message"
    destinations.clear()
    destinations["guest_direct_stay_b"] = "441234567890@c.us"
    assert registry.resolve_message(token, "guest_direct_stay_a") is None
    assert registry.resolve_message(token, "guest_direct_stay_b") is None


@pytest.mark.asyncio
async def test_managed_quote_token_waits_for_post_restart_verification(monkeypatch):
    """A restored group quote remains dormant until its exact route is ready."""
    store = FakeStore()
    route = "group_conversation_" + "a" * 32
    group_jid = "120363123456789@g.us"
    first = make_registry(monkeypatch, store=store)
    first.set_extra_route_resolver(
        lambda conversation_id: group_jid if conversation_id == route else None
    )
    await first.async_start()
    token = await first.async_remember_message("group-message-id", route)

    enabled = False
    restored = make_registry(monkeypatch, store=store)
    restored.set_extra_route_resolver(
        lambda conversation_id: (
            group_jid if enabled and conversation_id == route else None
        )
    )
    await restored.async_start()
    assert restored.resolve_message(token, route) is None

    enabled = True
    assert restored.resolve_message(token, route) == "group-message-id"


@pytest.mark.asyncio
async def test_configured_direct_chat_and_lid_routing(monkeypatch) -> None:
    """Only a unique configured direct contact resolves, including GOWS LIDs."""
    client = FakeClient({"123456789@lid": "393331234567@c.us"})
    registry = make_registry(monkeypatch, client=client)
    await registry.async_start()

    contact = await registry.async_resolve_chat("393331234567@c.us")
    assert contact is not None
    assert contact.recipient == "393331234567"
    assert contact.notify_entity_id == "notify.waha_sub-a"
    assert contact.person_entity_id == "person.seba"
    assert contact.event_sender() == {
        "contact_id": "sub-a",
        "notify_entity_id": "notify.waha_sub-a",
        "person_entity_id": "person.seba",
    }
    assert "393331234567" not in str(contact.event_sender())
    assert contact.conversation_id.startswith("direct_")
    assert "393331234567" not in contact.conversation_id
    assert registry.resolve_conversation(contact.conversation_id) == contact

    lid_contact = await registry.async_resolve_chat("123456789:2@lid")
    assert lid_contact == contact
    assert await registry.async_resolve_chat("393331234567@s.whatsapp.net") == contact
    assert client.lookups == ["123456789@lid"]
    assert await registry.async_resolve_chat("999999999999@c.us") is None
    assert await registry.async_resolve_chat("123456789@g.us") is None
    assert await registry.async_resolve_chat("name@s.whatsapp.net") is None
    assert await registry.async_resolve_chat("393331234567@g.us") is None
    assert await registry.async_resolve_chat("555555555@lid") is None

    guest = await registry.async_resolve_chat("441234567890@c.us")
    assert guest.event_sender() == {
        "contact_id": "sub-b",
        "notify_entity_id": "notify.waha_sub-b",
    }


@pytest.mark.asyncio
async def test_conversation_is_stable_across_rename_restart_and_unique_across_entries(
    monkeypatch,
) -> None:
    """A rename/restart keeps routing identity; entry IDs isolate contacts."""
    entry = make_entry()
    first = make_registry(monkeypatch, entry=entry)
    first_contact = await first.async_resolve_chat("393331234567@c.us")
    conversation_id = first_contact.conversation_id
    entry.subentries["sub-a"].title = "Sebastian"
    restarted = make_registry(monkeypatch, entry=entry)

    assert restarted.resolve_conversation(conversation_id).recipient == "393331234567"
    same_contact = await restarted.async_resolve_chat("393331234567@c.us")
    assert same_contact.conversation_id == conversation_id
    assert (
        make_registry(
            monkeypatch, entry=make_entry(entry_id="entry-b")
        ).resolve_conversation(conversation_id)
        is None
    )


@pytest.mark.asyncio
async def test_duplicate_phone_or_unconfigured_group_fails_closed(monkeypatch) -> None:
    """Ambiguous contacts and future group subentries cannot route a reply."""
    entry = make_entry()
    entry.subentries["sub-b"].data["recipient"] = "393331234567"
    entry.subentries["group"] = SimpleNamespace(
        subentry_id="group",
        subentry_type="group",
        data={"recipient": "393331234567"},
    )
    registry = make_registry(monkeypatch, entry=entry)

    assert await registry.async_resolve_chat("393331234567@c.us") is None
    assert (
        registry.resolve_conversation(
            registry._conversation_id("sub-a", "393331234567")
        )
        is None
    )
    assert (
        registry.resolve_conversation(
            registry._conversation_id("sub-b", "393331234567")
        )
        is None
    )
    assert (
        registry.resolve_conversation(
            registry._conversation_id("group", "393331234567")
        )
        is None
    )


@pytest.mark.asyncio
async def test_private_message_tracking_is_bounded_and_conversation_scoped(
    monkeypatch,
) -> None:
    """A public token cannot quote another contact's message or reveal WAHA IDs."""
    clock = {"now": 1_000_000.0}
    monkeypatch.setattr(registry_module.time, "time", lambda: clock["now"])
    store = FakeStore()
    registry = make_registry(monkeypatch, store=store)
    a = (await registry.async_resolve_chat("393331234567@c.us")).conversation_id
    b = (await registry.async_resolve_chat("441234567890@c.us")).conversation_id
    raw_id = "false_393331234567@c.us_ABCDEF"

    token = await registry.async_remember_message(raw_id, a)

    assert token.startswith("msg_")
    assert "ABCDEF" not in token
    assert registry.message_token("ABCDEF") == token
    assert registry.resolve_message(token, a) == raw_id
    assert registry.resolve_message(token, b) is None
    assert await registry.async_message_known("ABCDEF", a)
    assert not await registry.async_message_known("NOT-TRACKED", a)
    assert store.data["messages"][0]["raw_id"] == raw_id

    restarted = make_registry(monkeypatch, store=store)
    await restarted.async_start()
    assert restarted.resolve_message(token, a) == raw_id
    clock["now"] += registry_module.MESSAGE_TTL_SECONDS + 1
    assert restarted.resolve_message(token, a) is None
    assert not await restarted.async_message_known("ABCDEF", a)


@pytest.mark.asyncio
async def test_phone_edit_invalidates_old_quoted_reply_target(monkeypatch) -> None:
    """A delayed reply cannot route to a replacement phone number."""
    entry = make_entry()
    store = FakeStore()
    registry = make_registry(monkeypatch, entry=entry, store=store)
    contact = await registry.async_resolve_chat("393331234567@c.us")
    token = await registry.async_remember_message(
        "old-message", contact.conversation_id
    )
    entry.subentries["sub-a"].data["recipient"] = "393339999999"

    assert registry.resolve_conversation(contact.conversation_id) is None
    assert registry.resolve_message(token, contact.conversation_id) is None
    new_contact = await registry.async_resolve_chat("393339999999@c.us")
    assert new_contact.conversation_id != contact.conversation_id
    assert registry.resolve_message(token, new_contact.conversation_id) is None
    with pytest.raises(ValueError, match="Unknown WAHA conversation"):
        await registry.async_remember_message(
            "late-old-message", contact.conversation_id
        )

    restarted = make_registry(monkeypatch, entry=entry, store=store)
    await restarted.async_start()
    assert restarted.resolve_conversation(contact.conversation_id) is None
    assert restarted.resolve_message(token, contact.conversation_id) is None
    assert restarted.resolve_message(token, new_contact.conversation_id) is None


@pytest.mark.asyncio
async def test_event_claim_survives_restart_and_expires(monkeypatch) -> None:
    """Retries are suppressed with a private, bounded event-ID cache."""
    clock = {"now": 1_000_000.0}
    monkeypatch.setattr(registry_module.time, "time", lambda: clock["now"])
    store = FakeStore()
    registry = make_registry(monkeypatch, store=store)

    assert await registry.async_claim_event("evt_raw_private")
    assert not await registry.async_claim_event("evt_raw_private")
    assert store.data["events"][0]["token"] == registry.event_token("evt_raw_private")
    assert "evt_raw_private" not in str(store.data)
    restarted = make_registry(monkeypatch, store=store)
    await restarted.async_start()
    assert not await restarted.async_claim_event("evt_raw_private")
    clock["now"] += registry_module.EVENT_TTL_SECONDS + 1
    assert await restarted.async_claim_event("evt_raw_private")


@pytest.mark.asyncio
async def test_failed_save_rolls_back_claim_and_message_mapping(monkeypatch) -> None:
    """A WAHA retry can process an event after private storage recovers."""
    store = FakeStore()
    registry = make_registry(monkeypatch, store=store)
    contact = await registry.async_resolve_chat("393331234567@c.us")
    store.fail_saves = 1

    with pytest.raises(OSError, match="Storage unavailable"):
        await registry.async_claim_event("evt-retry")
    assert await registry.async_claim_event("evt-retry")
    assert not await registry.async_claim_event("evt-retry")

    store.fail_saves = 1
    token = registry.message_token("message-retry")
    with pytest.raises(OSError, match="Storage unavailable"):
        await registry.async_remember_message("message-retry", contact.conversation_id)
    assert registry.resolve_message(token, contact.conversation_id) is None
    assert (
        await registry.async_remember_message("message-retry", contact.conversation_id)
        == token
    )


@pytest.mark.asyncio
async def test_failed_load_can_switch_to_volatile_tracking(monkeypatch) -> None:
    """A failed restore must not write or trust the original disk store."""
    store = FakeStore({"messages": [{"token": "corrupt"}], "events": []})
    store.fail_loads = 1
    registry = make_registry(monkeypatch, store=store)
    contact = await registry.async_resolve_chat("393331234567@c.us")

    assert registry.persistence_available
    with pytest.raises(OSError, match="Storage unavailable"):
        await registry.async_start()

    registry.use_volatile_storage()
    assert not registry.persistence_available
    assert (
        registry.resolve_message(registry.message_token("old"), contact.conversation_id)
        is None
    )

    token = await registry.async_remember_message("new", contact.conversation_id)
    assert registry.resolve_message(token, contact.conversation_id) == "new"
    assert await registry.async_message_known("new", contact.conversation_id)
    assert await registry.async_claim_event("retry")
    assert not await registry.async_claim_event("retry")
    assert store.save_count == 0
    assert store.data == {"messages": [{"token": "corrupt"}], "events": []}


@pytest.mark.asyncio
async def test_volatile_fallback_clears_previously_loaded_state(monkeypatch) -> None:
    """A failed reload cannot leave earlier tracked state available."""
    store = FakeStore()
    registry = make_registry(monkeypatch, store=store)
    contact = await registry.async_resolve_chat("393331234567@c.us")
    old_token = await registry.async_remember_message("old", contact.conversation_id)
    assert await registry.async_claim_event("old-event")
    store.fail_loads = 1

    with pytest.raises(OSError, match="Storage unavailable"):
        await registry.async_start()
    registry.use_volatile_storage()

    assert registry.resolve_message(old_token, contact.conversation_id) is None
    assert await registry.async_claim_event("old-event")
    assert store.save_count == 2
    assert store.data["messages"][0]["raw_id"] == "old"

    # The replacement implements the same async load/save API, but its data
    # remains local to this registry instance.
    await registry.async_start()
    assert not await registry.async_claim_event("old-event")


@pytest.mark.asyncio
async def test_limits_prune_oldest_records_and_invalid_storage(monkeypatch) -> None:
    """Untrusted or stale stored state cannot grow the registry forever."""
    clock = {"now": 1_000_000.0}
    monkeypatch.setattr(registry_module.time, "time", lambda: clock["now"])
    monkeypatch.setattr(registry_module, "MAX_TRACKED_MESSAGES", 2)
    monkeypatch.setattr(registry_module, "MAX_CLAIMED_EVENTS", 2)
    store = FakeStore({"messages": [{"token": "bad"}], "events": ["bad"]})
    registry = make_registry(monkeypatch, store=store)
    await registry.async_start()
    contact = await registry.async_resolve_chat("393331234567@c.us")

    for index in range(3):
        await registry.async_remember_message(f"raw-{index}", contact.conversation_id)
        await registry.async_claim_event(f"event-{index}")
        clock["now"] += 1

    assert len(store.data["messages"]) == 2
    assert len(store.data["events"]) == 2
    assert (
        registry.resolve_message(
            registry.message_token("raw-0"), contact.conversation_id
        )
        is None
    )


@pytest.mark.asyncio
async def test_unknown_conversation_cannot_be_remembered(monkeypatch) -> None:
    """A fabricated routing ID never gets a message mapping."""
    registry = make_registry(monkeypatch)

    with pytest.raises(ValueError, match="Unknown WAHA conversation"):
        await registry.async_remember_message("ABCDEF", "direct_fake")


@pytest.mark.asyncio
async def test_cancelled_tracking_save_rolls_back_volatile_quote(monkeypatch):
    """Observer timeouts cannot leave an unpersisted quote usable in memory."""
    import asyncio

    class CancelledStore(FakeStore):
        async def async_save(self, data):
            raise asyncio.CancelledError

    registry = make_registry(monkeypatch, store=CancelledStore())
    contact = await registry.async_resolve_chat("393331234567@c.us")
    with pytest.raises(asyncio.CancelledError):
        await registry.async_remember_message("cancelled", contact.conversation_id)
    assert (
        registry.resolve_message(
            registry.message_token("cancelled"), contact.conversation_id
        )
        is None
    )
    with pytest.raises(asyncio.CancelledError):
        await registry.async_claim_event("cancelled-event")
    assert registry._events == {}
