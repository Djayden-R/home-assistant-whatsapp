"""Outbound observation isolation and notification recipient privacy."""

import asyncio
import sys
from types import ModuleType, SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from ._loader import PACKAGE_NAME, load_integration_module

outbound = load_integration_module("outbound")
api = load_integration_module("api")


class _Bus:
    def __init__(self):
        self.events = []

    def async_fire(self, event_type, data):
        self.events.append((event_type, data))


def _observer(*, enabled=True, bus=None, registry=None):
    bus = bus or _Bus()
    registry = registry or SimpleNamespace(
        async_remember_message=AsyncMock(return_value="msg_opaque")
    )
    entry = SimpleNamespace(
        entry_id="entry_opaque",
        options={"outbound_observations_enabled": enabled},
        runtime_data=SimpleNamespace(),
    )
    observer = outbound.OutboundObserver(SimpleNamespace(bus=bus), entry, registry)
    entry.runtime_data.outbound_observer = observer
    return observer, entry, bus, registry


@pytest.mark.asyncio
async def test_observes_recipient_content_quote_and_original_result():
    observer, _, bus, registry = _observer()
    result = api.WahaMessage("RAW_PRIVATE_MESSAGE", "RAW_PRIVATE_PHONE@c.us")
    send = AsyncMock(return_value=result)

    assert (
        await observer.async_send(
            "direct_opaque",
            "direct",
            {
                "kind": "text",
                "text": "Rendered recipient notice",
                "in_reply_to": "msg_quote",
            },
            send,
            origin="application",
            client_request_id="request_opaque",
        )
        is result
    )

    send.assert_awaited_once()
    registry.async_remember_message.assert_awaited_once_with(
        "RAW_PRIVATE_MESSAGE", "direct_opaque"
    )
    assert len(bus.events) == 1
    name, event = bus.events[0]
    assert name == "waha_whatsapp_event"
    assert event["schema_version"] == 1
    assert event["type"] == "message.outbound"
    assert event["conversation_id"] == "direct_opaque"
    assert event["conversation_type"] == "direct"
    assert event["origin"] == "application"
    assert event["client_request_id"] == "request_opaque"
    assert event["status"] == "accepted"
    assert event["message"] == {
        "id": "msg_opaque",
        "kind": "text",
        "text": "Rendered recipient notice",
        "in_reply_to": "msg_quote",
    }
    assert "RAW_PRIVATE" not in str(bus.events)
    assert observer.health == "ready"


@pytest.mark.asyncio
@pytest.mark.parametrize("redacted", [False, True])
async def test_poll_options_and_explicit_redaction(redacted):
    observer, _, bus, _ = _observer()
    await observer.async_send(
        "direct_opaque",
        "direct",
        {
            "kind": "poll",
            "text": "PRIVATE_POLL",
            "options": ["PRIVATE_OPTION"],
            "provider_secret": "PRIVATE_SECRET",
        },
        AsyncMock(return_value=api.WahaMessage(None, "private")),
        content_redacted=redacted,
    )
    event = bus.events[0][1]
    assert "PRIVATE_SECRET" not in str(event)
    if redacted:
        assert event["content_redacted"] is True
        assert event["message"] == {"id": None, "kind": "poll", "in_reply_to": None}
        assert "PRIVATE" not in str(event)
    else:
        assert event["message"]["text"] == "PRIVATE_POLL"
        assert event["message"]["options"] == ["PRIVATE_OPTION"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("error", "status"),
    [
        (api.WahaRequestError("PRIVATE_REJECTED", 400), "failed"),
        (api.WahaRequestError("PRIVATE_BACKEND_FAILURE", 503), "unknown"),
        (api.WahaAuthenticationError("PRIVATE_API_KEY"), "failed"),
        (api.WahaConnectionError("PRIVATE_URL"), "unknown"),
        (api.WahaResponseError("PRIVATE_RESPONSE"), "unknown"),
        (RuntimeError("PRIVATE_PROVIDER"), "unknown"),
        (asyncio.CancelledError(), "unknown"),
    ],
)
async def test_send_failures_are_classified_and_propagated_unchanged(error, status):
    observer, _, bus, _ = _observer()
    with pytest.raises(type(error)) as caught:
        await observer.async_send(
            "direct_opaque",
            "direct",
            {"kind": "text", "text": "notice"},
            AsyncMock(side_effect=error),
        )
    assert caught.value is error
    assert len(bus.events) == 1
    assert bus.events[0][1]["status"] == status
    assert bus.events[0][1]["message"]["id"] is None
    assert "PRIVATE" not in str(observer.snapshot())
    assert "PRIVATE" not in str(bus.events)


@pytest.mark.asyncio
async def test_malformed_result_is_unknown_without_changing_callback_result():
    observer, _, bus, _ = _observer()
    assert (
        await observer.async_send(
            "direct_opaque",
            "direct",
            {"kind": "text", "text": "notice"},
            AsyncMock(return_value=None),
        )
        is None
    )
    assert bus.events[0][1]["status"] == "unknown"


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["accepted", "unknown", "PRIVATE_BAD_STATUS"])
async def test_message_without_id_preserves_explicit_transport_outcome(outcome):
    observer, _, bus, registry = _observer()
    result = api.WahaMessage(None, "private", outcome=outcome)
    send = AsyncMock(return_value=result)
    assert (
        await observer.async_send(
            "direct_opaque", "direct", {"kind": "text", "text": "notice"}, send
        )
        is result
    )
    send.assert_awaited_once()
    registry.async_remember_message.assert_not_awaited()
    assert bus.events[0][1]["status"] == (
        "accepted" if outcome == "accepted" else "unknown"
    )
    assert bus.events[0][1]["message"]["id"] is None
    assert "PRIVATE_BAD_STATUS" not in str(bus.events)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "error", [RuntimeError("PRIVATE_STORE"), asyncio.CancelledError()]
)
async def test_accepted_but_tracking_failed_has_null_message_id(error):
    registry = SimpleNamespace(async_remember_message=AsyncMock(side_effect=error))
    observer, _, bus, _ = _observer(registry=registry)
    result = api.WahaMessage("private", "private")
    assert (
        await observer.async_send(
            "direct_opaque",
            "direct",
            {"kind": "text", "text": "notice"},
            AsyncMock(return_value=result),
        )
        is result
    )
    assert bus.events[0][1]["status"] == "accepted"
    assert bus.events[0][1]["message"]["id"] is None
    assert observer.health == "degraded"
    assert observer.snapshot()["tracking_failures"] == 1
    assert "PRIVATE" not in str(observer.snapshot())


@pytest.mark.asyncio
async def test_event_bus_failure_never_changes_delivery_or_original_exception():
    def broken_bus(*_args):
        raise RuntimeError("PRIVATE_BUS_DATA")

    observer, _, _, _ = _observer(bus=SimpleNamespace(async_fire=broken_bus))
    result = api.WahaMessage(None, "private")
    assert (
        await observer.async_send(
            "direct_opaque",
            "direct",
            {"kind": "text", "text": "notice"},
            AsyncMock(return_value=result),
        )
        is result
    )
    error = api.WahaConnectionError("PRIVATE_URL")
    with pytest.raises(api.WahaConnectionError) as caught:
        await observer.async_send(
            "direct_opaque",
            "direct",
            {"kind": "text", "text": "notice"},
            AsyncMock(side_effect=error),
        )
    assert caught.value is error
    assert observer.snapshot()["observation_failures"] == 2
    assert observer.health == "degraded"
    assert "PRIVATE" not in str(observer.snapshot())


@pytest.mark.asyncio
async def test_observations_default_off_but_legacy_tracking_remains():
    observer, entry, bus, registry = _observer(enabled=False)
    entry.options = {}
    await observer.async_send(
        "direct_opaque",
        "direct",
        {"kind": "text", "text": "notice"},
        AsyncMock(return_value=api.WahaMessage("private", "private")),
    )
    assert not bus.events
    registry.async_remember_message.assert_awaited_once()
    assert observer.health == "unavailable"


@pytest.mark.asyncio
async def test_no_route_still_sends_without_guessing_a_contact():
    observer, _, bus, registry = _observer()
    send = AsyncMock(return_value=api.WahaMessage("private", "phone@c.us"))
    await observer.async_send(None, "direct", {"kind": "text", "text": "notice"}, send)
    send.assert_awaited_once()
    assert not bus.events
    registry.async_remember_message.assert_not_awaited()
    assert observer.health == "degraded"


@pytest.mark.asyncio
@pytest.mark.parametrize("after_send", [False, True])
async def test_broken_observer_does_not_block_or_duplicate_notification(after_send):
    _, entry, _, _ = _observer()
    result = api.WahaMessage("private", "private")
    send = AsyncMock(return_value=result)

    async def broken(_route, _type, _content, callback, **_metadata):
        if after_send:
            await callback()
        raise RuntimeError("PRIVATE_OBSERVER_STATE")

    entry.runtime_data.outbound_observer = SimpleNamespace(async_send=broken)
    assert (
        await outbound.async_send_observed(
            entry, "direct_opaque", "direct", {"kind": "text", "text": "notice"}, send
        )
        is result
    )
    send.assert_awaited_once()


@pytest.mark.asyncio
async def test_broken_observer_cannot_resubmit_uncertain_attempt():
    _, entry, _, _ = _observer()
    error = api.WahaConnectionError("private")
    send = AsyncMock(side_effect=error)

    async def broken(_route, _type, _content, callback):
        try:
            await callback()
        except api.WahaConnectionError:
            await callback()

    entry.runtime_data.outbound_observer = SimpleNamespace(async_send=broken)
    with pytest.raises(api.WahaConnectionError) as caught:
        await outbound.async_send_observed(
            entry, "direct_opaque", "direct", {"kind": "text", "text": "notice"}, send
        )
    assert caught.value is error
    send.assert_awaited_once()


@pytest.mark.asyncio
async def test_cancelled_observer_before_callback_does_not_initiate_send():
    _, entry, _, _ = _observer()
    send = AsyncMock(return_value=api.WahaMessage(None, "private"))
    entry.runtime_data.outbound_observer.async_send = AsyncMock(
        side_effect=asyncio.CancelledError()
    )
    with pytest.raises(asyncio.CancelledError):
        await outbound.async_send_observed(
            entry, "direct_opaque", "direct", {"kind": "text", "text": "notice"}, send
        )
    send.assert_not_awaited()


@pytest.mark.asyncio
async def test_observer_wrapper_failure_marks_health_degraded():
    observer, entry, _, _ = _observer()
    observer.async_send = AsyncMock(side_effect=RuntimeError("PRIVATE_DETAILS"))
    send = AsyncMock(return_value=api.WahaMessage(None, "private"))
    await outbound.async_send_observed(
        entry, "direct_opaque", "direct", {"kind": "text", "text": "notice"}, send
    )
    send.assert_awaited_once()
    assert observer.health == "degraded"
    assert observer.snapshot()["observation_failures"] == 1
    assert "PRIVATE_DETAILS" not in str(observer.snapshot())


@pytest.mark.asyncio
async def test_observer_return_without_callback_cannot_silently_drop_notification():
    observer, entry, _, _ = _observer()
    observer.async_send = AsyncMock(return_value=None)
    result = api.WahaMessage(None, "private")
    send = AsyncMock(return_value=result)
    assert (
        await outbound.async_send_observed(
            entry, "direct_opaque", "direct", {"kind": "text", "text": "notice"}, send
        )
        is result
    )
    send.assert_awaited_once()
    assert observer.health == "degraded"


@pytest.mark.asyncio
async def test_observer_cannot_replace_actual_delivery_failure_with_success():
    observer, entry, _, _ = _observer()
    error = api.WahaConnectionError("PRIVATE_BACKEND")
    send = AsyncMock(side_effect=error)

    async def swallowed(_route, _type, _content, callback):
        try:
            await callback()
        except api.WahaConnectionError:
            return api.WahaMessage("pretended", "private")

    observer.async_send = swallowed
    with pytest.raises(api.WahaConnectionError) as caught:
        await outbound.async_send_observed(
            entry, "direct_opaque", "direct", {"kind": "text", "text": "notice"}, send
        )
    assert caught.value is error
    send.assert_awaited_once()


def _notify_module(monkeypatch):
    """Load the notify platform using minimal HA entity interface stubs."""
    modules = {
        "homeassistant.components.notify": {
            "NotifyEntity": object,
            "NotifyEntityFeature": SimpleNamespace(TITLE=1),
        },
        "homeassistant.config_entries": {"ConfigSubentry": object},
        "homeassistant.core": {"HomeAssistant": object},
        "homeassistant.exceptions": {"HomeAssistantError": RuntimeError},
        "homeassistant.helpers.device_registry": {
            "DeviceEntryType": SimpleNamespace(SERVICE="service"),
            "DeviceInfo": dict,
        },
        "homeassistant.helpers.entity_platform": {
            "AddConfigEntryEntitiesCallback": object
        },
    }
    for name, attributes in modules.items():
        module = ModuleType(name)
        module.__dict__.update(attributes)
        monkeypatch.setitem(sys.modules, name, module)
    monkeypatch.setattr(
        sys.modules[PACKAGE_NAME], "WahaConfigEntry", object, raising=False
    )
    return load_integration_module("notify")


@pytest.mark.asyncio
async def test_notify_observes_configured_recipient_and_recipient_rendering(
    monkeypatch,
):
    notify = _notify_module(monkeypatch)
    observer, entry, bus, registry = _observer()
    configured = "15555550123"
    subentry = SimpleNamespace(
        subentry_id="contact",
        data={"recipient": configured},
    )
    registry.async_resolve_chat = AsyncMock(
        return_value=SimpleNamespace(
            subentry_id="contact",
            conversation_id="direct_configured",
        )
    )
    entry.runtime_data.channel_registry = registry
    # Even a backend response claiming another recipient cannot alter the route.
    entry.runtime_data.client = SimpleNamespace(
        async_send_text=AsyncMock(
            return_value=api.WahaMessage("private", "15555550999@c.us")
        )
    )
    await notify._async_send_to_recipient(entry, subentry, "Body", "Title")
    registry.async_resolve_chat.assert_awaited_once_with(configured)
    event = bus.events[0][1]
    assert event["conversation_id"] == "direct_configured"
    assert event["message"]["text"] == "*Title*\n\nBody"
    assert observer.observed_count == 1


@pytest.mark.asyncio
async def test_notify_delivers_when_route_observer_is_broken(monkeypatch):
    notify = _notify_module(monkeypatch)
    _, entry, _, registry = _observer()
    registry.async_resolve_chat = AsyncMock(side_effect=RuntimeError("PRIVATE"))
    entry.runtime_data.channel_registry = registry
    entry.runtime_data.outbound_observer = None
    send = AsyncMock(return_value=api.WahaMessage(None, "private"))
    entry.runtime_data.client = SimpleNamespace(async_send_text=send)
    subentry = SimpleNamespace(subentry_id="contact", data={"recipient": "15555550123"})
    await notify._async_send_to_recipient(entry, subentry, "Urgent", None)
    send.assert_awaited_once_with("15555550123", "Urgent")


@pytest.mark.asyncio
async def test_notification_redaction_and_unrelated_subentry_isolation(monkeypatch):
    notify = _notify_module(monkeypatch)
    _, entry, bus, registry = _observer()
    registry.async_resolve_chat = AsyncMock(
        return_value=SimpleNamespace(
            subentry_id="contact", conversation_id="direct_configured"
        )
    )
    entry.runtime_data.channel_registry = registry
    send = AsyncMock(return_value=api.WahaMessage(None, "private"))
    entry.runtime_data.client = SimpleNamespace(async_send_text=send)
    subentry = SimpleNamespace(
        subentry_id="contact",
        data={"recipient": "15555550123", "observation_content_redacted": True},
    )
    await notify._async_send_to_recipient(entry, subentry, "PRIVATE_PIN", None)
    send.assert_awaited_once_with("15555550123", "PRIVATE_PIN")
    assert bus.events[0][1]["content_redacted"] is True
    assert "PRIVATE_PIN" not in str(bus.events)

    # A registry mapping for another contact never supplies an observed route.
    registry.async_resolve_chat.return_value.subentry_id = "different_contact"
    await notify._async_send_to_recipient(entry, subentry, "Another", None)
    assert send.await_count == 2
    assert len(bus.events) == 1


@pytest.mark.asyncio
async def test_general_group_notify_uses_distinct_route_and_content_redaction(
    monkeypatch,
):
    notify = _notify_module(monkeypatch)
    _, entry, bus, _ = _observer()
    send = AsyncMock(return_value=api.WahaMessage(None, "private@g.us"))
    destination = SimpleNamespace(conversation_id="group_configured")
    entry.runtime_data.general_groups = SimpleNamespace(
        resolve_subentry=lambda _subentry: destination, async_send_text=send
    )
    entity = object.__new__(notify.WahaGeneralGroupNotifyEntity)
    entity.config_entry = entry
    entity._subentry = SimpleNamespace(
        subentry_id="group", data={"observation_content_redacted": True}
    )
    assert entity.available
    await entity.async_send_message("PRIVATE_GROUP_NOTICE", "Title")
    send.assert_awaited_once_with("group_configured", "*Title*\n\nPRIVATE_GROUP_NOTICE")
    assert len(bus.events) == 1
    assert bus.events[0][1]["conversation_id"] == "group_configured"
    assert bus.events[0][1]["conversation_type"] == "group"
    assert bus.events[0][1]["content_redacted"] is True
    assert "PRIVATE_GROUP_NOTICE" not in str(bus.events)


@pytest.mark.asyncio
async def test_managed_guest_notify_uses_independent_route(monkeypatch):
    notify = _notify_module(monkeypatch)
    _, entry, bus, _ = _observer()
    send = AsyncMock(return_value=api.WahaMessage(None, "private@g.us"))
    entry.runtime_data.guest_manager = SimpleNamespace(async_send_to_group=send)
    entry.runtime_data.guest_registry = SimpleNamespace(
        snapshot=lambda: {"conversation_id": "managed_guest_group"}
    )
    entity = object.__new__(notify.WahaGuestGroupNotifyEntity)
    entity.config_entry = entry
    await entity.async_send_message("Guest notice")
    send.assert_awaited_once_with("Guest notice")
    assert bus.events[0][1]["conversation_id"] == "managed_guest_group"
    assert bus.events[0][1]["message"]["text"] == "Guest notice"
    entry.options["guest_group_observation_content_redacted"] = True
    await entity.async_send_message("PRIVATE_GUEST_CONTENT")
    assert bus.events[1][1]["content_redacted"] is True
    assert "PRIVATE_GUEST_CONTENT" not in str(bus.events[1])


@pytest.mark.asyncio
async def test_stalled_tracking_is_bounded_and_does_not_change_acceptance(monkeypatch):
    monkeypatch.setattr(outbound, "_OBSERVATION_TIMEOUT_SECONDS", 0.001)

    async def stalled(*_args):
        await asyncio.Event().wait()

    registry = SimpleNamespace(async_remember_message=stalled)
    observer, _, bus, _ = _observer(registry=registry)
    result = api.WahaMessage("private", "private")
    assert (
        await observer.async_send(
            "direct_opaque",
            "direct",
            {"kind": "text", "text": "notice"},
            AsyncMock(return_value=result),
        )
        is result
    )
    assert bus.events[0][1]["status"] == "accepted"
    assert bus.events[0][1]["message"]["id"] is None
    assert observer.health == "degraded"
