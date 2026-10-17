"""Voice metadata stays additive across all existing inbound normalizers."""

from copy import deepcopy
from time import time
from types import SimpleNamespace

from ._loader import load_integration_module
from .test_general_group_inbound import envelope as group_envelope
from .test_general_group_inbound import make_inbound
from .test_inbound import _event, _setup
from .test_media_core import manager


async def test_direct_native_voice_with_media_null_keeps_audio_and_dedup(tmp_path):
    inbound, _, bus = _setup()
    transport = await manager(tmp_path, entry_id=inbound._entry.entry_id)
    inbound._media_manager = transport
    event = _event(
        timestamp=time(),
        body="",
        hasMedia=True,
        media=None,
        _data={"Info": {"MediaType": "ptt"}},
    )
    await inbound.async_handle_payload(event)
    await inbound.async_handle_payload(deepcopy(event))
    assert len(bus.fired) == 1
    message = bus.fired[0][1]["message"]
    assert message["kind"] == "audio"
    assert message["attachments"][0]["voice_note"] is True
    assert message["attachments"][0]["mime_type"] is None
    assert message["attachments"][0]["retrievable"] is True
    assert bus.fired[0][1]["sender"]["contact_id"] == "contact_abc"
    assert len(transport.store.records) == 1


async def test_enabled_group_voice_preserves_speaker_mentions_quote_and_dedup(tmp_path):
    inbound, _, entry, _, registry, events = make_inbound()
    transport = await manager(tmp_path, entry_id=entry.entry_id)
    inbound._media_manager = transport
    event = group_envelope(
        body="", hasMedia=True, media=None, _data={"Info": {"MediaType": "ptt"}}
    )
    assert await inbound.async_handle_payload(event)
    assert not await inbound.async_handle_payload(deepcopy(event))
    assert len(events) == 1
    message = events[0]["message"]
    assert message["kind"] == "audio"
    assert message["attachments"][0]["voice_note"] is True
    assert events[0]["sender"]["identity_status"] == "verified"
    assert message["mentions"] == {
        "available": False,
        "bot": None,
        "participant_ids": [],
    }
    assert message["in_reply_to"] is None
    assert len(transport.store.records) == 1


async def test_managed_guest_voice_with_media_null_preserves_stay_and_speaker(tmp_path):
    registry_module = load_integration_module("guest_registry")
    inbound_module = load_integration_module("guest_inbound")
    from .test_general_groups import FakeStore
    from .test_inbound import _Registry

    bot, guest, group = "390000000099@c.us", "390000000001@c.us", "120000000000@g.us"
    guests = registry_module.GuestRegistry(None, "entry", store=FakeStore())
    await guests.async_initialize_empty()
    await guests.async_start()
    await guests.async_bind_account(bot)
    await guests.async_set_group(group)
    await guests.async_set_provisioning_status("ready")
    await guests.async_reconcile(
        [
            {
                "aliases": [guest],
                "aliases_verified": False,
                "kind": "guest",
                "role": "participant",
            }
        ],
        observed_at=time() - 10,
        verified_bot_jid=bot,
        security_verified=True,
        bot_admin_verified=True,
    )
    events = []
    hass = SimpleNamespace(
        bus=SimpleNamespace(async_fire=lambda event, data: events.append(data))
    )
    entry = SimpleNamespace(entry_id="entry", data={"session": "house"})
    transport = await manager(tmp_path)
    inbound = inbound_module.WahaGuestInboundManager(
        hass, entry, guests, _Registry(), verified_bot_jid=bot, media_manager=transport
    )
    event = {
        "session": "house",
        "event": "message",
        "id": "event",
        "payload": {
            "id": "rawmsg",
            "from": group,
            "participant": guest,
            "fromMe": False,
            "timestamp": time(),
            "body": "",
            "hasMedia": True,
            "media": None,
            "_data": {"Info": {"MediaType": "ptt"}},
        },
    }
    assert await inbound.async_handle_payload(event)
    assert not await inbound.async_handle_payload(deepcopy(event))
    assert len(events) == 1
    message = events[0]["message"]
    assert message["kind"] == "audio"
    assert message["attachments"][0]["voice_note"] is True
    assert events[0]["sender"]["participant_id"].startswith("participant_")
    assert events[0]["membership"]["status"] == "active"
    assert events[0]["membership"]["membership_id"].startswith("membership_")


async def test_guest_stay_change_during_message_persistence_cannot_rebind_media(
    tmp_path,
):
    import asyncio
    from functools import partial
    from unittest.mock import AsyncMock

    from .test_general_groups import FakeStore
    from .test_inbound import _Registry
    from .test_media_routes import _entry

    media = load_integration_module("media")
    routes = load_integration_module("media_routes")
    registry_module = load_integration_module("guest_registry")
    inbound_module = load_integration_module("guest_inbound")
    bot, guest, group = "390000000099@c.us", "390000000001@c.us", "120000000000@g.us"
    guests = registry_module.GuestRegistry(None, "entry", store=FakeStore())
    await guests.async_initialize_empty()
    await guests.async_start()
    await guests.async_bind_account(bot)
    await guests.async_set_group(group)
    await guests.async_set_provisioning_status("ready")
    roster = [
        {
            "aliases": [guest],
            "aliases_verified": False,
            "kind": "guest",
            "role": "participant",
        }
    ]

    async def reconcile(roster):
        await guests.async_reconcile(
            roster,
            observed_at=time(),
            verified_bot_jid=bot,
            security_verified=True,
            bot_admin_verified=True,
        )

    await reconcile(roster)
    original = guests.resolve_member(guest, occurred_at=time())
    entry = _entry()
    entry.entry_id = "entry"
    entry.runtime_data.channel_registry.resolve_conversation = lambda _id: None
    entry.runtime_data.guest_registry = guests
    entry.runtime_data.guest_manager = SimpleNamespace(
        async_reconcile=AsyncMock(return_value={"ready": True}),
        async_verify_group_destination=AsyncMock(return_value=True),
        async_verify_guest_destination=AsyncMock(return_value=True),
    )
    harness = await manager(tmp_path)
    transport = media.MediaManager(
        harness.hass,
        entry,
        None,
        authorize=partial(routes.async_authorize_media, entry),
        backend_ready=partial(routes.async_media_backend_ready, entry),
        observe_binding=partial(routes.media_observation_binding, entry),
    )
    await transport.async_start()
    events = []
    hass = SimpleNamespace(
        bus=SimpleNamespace(async_fire=lambda event, data: events.append(data))
    )
    channel = _Registry()
    entered, release = asyncio.Event(), asyncio.Event()
    remember = channel.async_remember_message

    async def blocked_remember(*args):
        entered.set()
        await release.wait()
        return await remember(*args)

    channel.async_remember_message = blocked_remember
    inbound = inbound_module.WahaGuestInboundManager(
        hass, entry, guests, channel, verified_bot_jid=bot, media_manager=transport
    )
    event = {
        "session": "default",
        "event": "message",
        "id": "event",
        "payload": {
            "id": "rawmsg",
            "from": group,
            "participant": guest,
            "fromMe": False,
            "timestamp": time(),
            "body": "",
            "hasMedia": True,
            "media": None,
            "_data": {"Info": {"MediaType": "ptt"}},
        },
    }
    task = asyncio.create_task(inbound.async_handle_payload(event))
    await asyncio.wait_for(entered.wait(), 2)
    await reconcile([])
    await reconcile(roster)
    current = guests.resolve_member(guest, occurred_at=time())
    assert current["membership_id"] != original["membership_id"]
    release.set()
    assert await task
    assert len(events) == 1
    assert events[0]["membership"]["membership_id"] == original["membership_id"]
    assert events[0]["message"]["attachments"][0]["retrievable"] is False
    assert not transport.store.records
