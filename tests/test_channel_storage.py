"""Optional tracking reports actual HA disk failures without changing delivery."""

import asyncio
import json
import threading
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

pytest.importorskip("homeassistant")

from homeassistant.core import HomeAssistant  # noqa: E402
from homeassistant.helpers.storage import Store  # noqa: E402
from homeassistant.util import file as file_util  # noqa: E402

from ._loader import load_integration_module  # noqa: E402

api = load_integration_module("api")
channel = load_integration_module("channel_registry")
outbound = load_integration_module("outbound")


def _entry():
    return SimpleNamespace(
        entry_id="entry",
        data={"webhook_secret": "private-test-key"},
        options={"outbound_observations_enabled": True},
        subentries={
            "contact": SimpleNamespace(
                subentry_id="contact",
                subentry_type="recipient",
                data={"recipient": "393331234567"},
            )
        },
    )


@pytest.mark.asyncio
async def test_actual_tracking_disk_failure_preserves_send_and_degrades_health(
    tmp_path, monkeypatch, caplog
):
    hass = HomeAssistant(str(tmp_path))
    entry = _entry()
    monkeypatch.setattr(channel, "_notify_entity_id", lambda *_args: None)
    registry = channel.ChannelRegistry(
        hass, entry, SimpleNamespace(session_name="default")
    )
    await registry.async_start()
    contact = await registry.async_resolve_chat("393331234567@c.us")
    observer = outbound.OutboundObserver(hass, entry, registry)
    events = []
    hass.bus.async_listen(
        "waha_whatsapp_event", lambda event: events.append(event.data)
    )

    def fail_write(*_args, **_kwargs):
        raise file_util.WriteError("private disk failure")

    monkeypatch.setattr(file_util, "write_utf8_file_atomic", fail_write)
    send = AsyncMock(return_value=api.WahaMessage("provider-id", "393331234567@c.us"))
    result = await observer.async_send(
        contact.conversation_id,
        "direct",
        {"kind": "text", "text": "private notice content"},
        send,
    )
    await hass.async_block_till_done()
    send.assert_awaited_once()
    assert result.id == "provider-id"
    assert observer.health == "degraded"
    assert observer.tracking_failure_count == 1
    assert events[0]["status"] == "accepted"
    assert events[0]["message"]["id"] is None
    assert "private notice content" not in caplog.text
    assert "private disk failure" not in caplog.text


@pytest.mark.asyncio
async def test_channel_loads_existing_ha_store_envelope(tmp_path, monkeypatch):
    """A strict writer preserves quote mappings written by the old Store format."""
    hass = HomeAssistant(str(tmp_path))
    entry = _entry()
    monkeypatch.setattr(channel, "_notify_entity_id", lambda *_args: None)
    client = SimpleNamespace(session_name="default")
    registry = channel.ChannelRegistry(hass, entry, client)
    await registry.async_start()
    contact = await registry.async_resolve_chat("393331234567@c.us")
    quote = await registry.async_remember_message(
        "provider-id", contact.conversation_id
    )
    old = Store(
        hass, 1, "waha_whatsapp.channel.entry", private=True, atomic_writes=True
    )
    data = await old.async_load()
    await old.async_save(data)
    restored = channel.ChannelRegistry(hass, entry, client)
    await restored.async_start()
    assert restored.resolve_message(quote, contact.conversation_id) == "provider-id"


@pytest.mark.asyncio
async def test_timed_out_physical_tracking_write_cannot_overwrite_reloaded_claims(
    tmp_path, monkeypatch
):
    hass = HomeAssistant(str(tmp_path))
    entry = _entry()
    monkeypatch.setattr(channel, "_notify_entity_id", lambda *_args: None)
    monkeypatch.setattr(outbound, "_OBSERVATION_TIMEOUT_SECONDS", 0.01)
    client = SimpleNamespace(session_name="default")
    old = channel.ChannelRegistry(hass, entry, client)
    await old.async_start()
    route = (await old.async_resolve_chat("393331234567@c.us")).conversation_id
    observer = outbound.OutboundObserver(hass, entry, old)
    original = file_util.write_utf8_file_atomic
    entered = threading.Event()
    release = threading.Event()
    writes = 0

    def stall_first(*args, **kwargs):
        nonlocal writes
        writes += 1
        if writes == 1:
            entered.set()
            assert release.wait(5)
        return original(*args, **kwargs)

    monkeypatch.setattr(file_util, "write_utf8_file_atomic", stall_first)
    callback = AsyncMock(
        return_value=api.WahaMessage("old-provider-id", "393331234567@c.us")
    )
    result = await observer.async_send(
        route, "direct", {"kind": "text", "text": "notice"}, callback
    )
    assert entered.is_set()
    assert result.id == "old-provider-id"
    token = old.message_token(result.id)
    assert old.resolve_message(token, route) is None
    replacement = channel.ChannelRegistry(hass, entry, client)
    start = asyncio.create_task(replacement.async_start())
    await asyncio.sleep(0)
    stopping = asyncio.create_task(old.async_stop())
    await asyncio.sleep(0)
    stopping.cancel()
    with pytest.raises(asyncio.CancelledError):
        await stopping
    try:
        assert not start.done()
        with pytest.raises(RuntimeError, match="stopped"):
            await old.async_claim_event("stale-claim")
    finally:
        release.set()
    await start
    await observer.async_stop()
    assert replacement.resolve_message(token, route) == "old-provider-id"
    assert await replacement.async_claim_event("new-claim-B")
    third = channel.ChannelRegistry(hass, entry, client)
    await third.async_start()
    assert not await third.async_claim_event("new-claim-B")
    assert third.resolve_message(token, route) == "old-provider-id"
    path = Path(hass.config.path(".storage", "waha_whatsapp.channel.entry"))
    saved = json.loads(await asyncio.to_thread(path.read_bytes))["data"]
    assert len(saved["events"]) == 1
    callback.assert_awaited_once()


@pytest.mark.asyncio
async def test_tracking_deadline_returns_no_quote_before_late_disk_failure(
    tmp_path, monkeypatch, caplog
):
    from .test_channel_services import _handlers

    hass = HomeAssistant(str(tmp_path))
    entry = _entry()
    monkeypatch.setattr(channel, "_notify_entity_id", lambda *_args: None)
    monkeypatch.setattr(outbound, "_OBSERVATION_TIMEOUT_SECONDS", 0.01)
    client = SimpleNamespace(
        session_name="default",
        async_send_text=AsyncMock(
            return_value=api.WahaMessage("provider-id", "393331234567@c.us")
        ),
    )
    registry = channel.ChannelRegistry(hass, entry, client)
    await registry.async_start()
    route = (await registry.async_resolve_chat("393331234567@c.us")).conversation_id
    observer = outbound.OutboundObserver(hass, entry, registry)
    correlated = load_integration_module("correlated").CorrelatedSendManager(
        hass, entry
    )
    await correlated.async_start()
    entry.state = "loaded"
    entry.runtime_data = SimpleNamespace(
        channel_registry=registry,
        client=client,
        outbound_observer=observer,
        correlated_sends=correlated,
        guest_registry=SimpleNamespace(
            resolve_group_conversation=lambda _route: None,
            resolve_route=lambda _route: None,
        ),
        general_groups=SimpleNamespace(resolve_conversation=lambda _route: None),
    )
    service_hass = SimpleNamespace(
        config_entries=SimpleNamespace(async_entries=lambda _domain: [entry])
    )
    entered = threading.Event()
    release = threading.Event()
    original = file_util.write_utf8_file_atomic

    def delayed_channel_failure(path, *args, **kwargs):
        if str(path).endswith("waha_whatsapp.channel.entry"):
            entered.set()
            assert release.wait(5)
            raise file_util.WriteError("private late disk failure")
        return original(path, *args, **kwargs)

    monkeypatch.setattr(file_util, "write_utf8_file_atomic", delayed_channel_failure)
    try:
        result = await _handlers()["_async_handle_send_to_conversation"](
            SimpleNamespace(
                hass=service_hass,
                data={
                    "conversation_id": route,
                    "message": "private notice",
                    "client_request_id": "request",
                },
                return_response=True,
            )
        )
        assert entered.is_set()
        assert result["status"] == "accepted"
        assert result["message_id"] is None
        assert observer.health == "degraded"
        assert (
            registry.resolve_message(registry.message_token("provider-id"), route)
            is None
        )
    finally:
        release.set()
    await observer.async_stop()
    await registry.async_stop()
    assert (
        registry.resolve_message(registry.message_token("provider-id"), route) is None
    )
    assert observer.health == "unavailable"
    assert observer.tracking_failure_count >= 1
    assert "private late disk failure" not in caplog.text
    assert "private notice" not in caplog.text
    client.async_send_text.assert_awaited_once()


@pytest.mark.asyncio
async def test_stopped_replacement_waiting_for_physical_write_never_revives(
    tmp_path, monkeypatch
):
    hass = HomeAssistant(str(tmp_path))
    entry = _entry()
    monkeypatch.setattr(channel, "_notify_entity_id", lambda *_args: None)
    client = SimpleNamespace(session_name="default")
    old = channel.ChannelRegistry(hass, entry, client)
    await old.async_start()
    entered = threading.Event()
    release = threading.Event()
    original = file_util.write_utf8_file_atomic

    def stall(*args, **kwargs):
        entered.set()
        assert release.wait(5)
        return original(*args, **kwargs)

    monkeypatch.setattr(file_util, "write_utf8_file_atomic", stall)
    claim = asyncio.create_task(old.async_claim_event("old-claim"))
    assert await asyncio.to_thread(entered.wait, 3)
    replacement = channel.ChannelRegistry(hass, entry, client)
    start = asyncio.create_task(replacement.async_start())
    await asyncio.sleep(0)
    replacement.stop()
    release.set()
    assert await claim
    await start
    assert replacement.persistence_available is False
    assert replacement._events == {}
    with pytest.raises(RuntimeError, match="stopped"):
        await replacement.async_claim_event("new-claim")
    third = channel.ChannelRegistry(hass, entry, client)
    await third.async_start()
    assert not await third.async_claim_event("old-claim")
