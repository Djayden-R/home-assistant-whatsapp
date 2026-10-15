"""Individual notify entities for WAHA WhatsApp."""

from contextlib import suppress
from typing import override

from homeassistant.components.notify import NotifyEntity, NotifyEntityFeature
from homeassistant.config_entries import ConfigSubentry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import WahaConfigEntry
from .api import WahaError
from .const import (
    CONF_GUEST_GROUP_ENABLED,
    CONF_GUEST_GROUP_NAME,
    CONF_RECIPIENT,
    DEFAULT_GUEST_GROUP_NAME,
    DOMAIN,
)
from .general_groups import SUBENTRY_TYPE_GROUP, GeneralGroupError
from .guest_manager import GuestGroupError
from .helpers import contact_state_attributes, render_notification
from .outbound import async_send_observed


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: WahaConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up individual WhatsApp recipients."""
    for subentry_id, subentry in config_entry.subentries.items():
        async_add_entities(
            [
                WahaGeneralGroupNotifyEntity(config_entry, subentry)
                if subentry.subentry_type == SUBENTRY_TYPE_GROUP
                else WahaNotifyEntity(config_entry, subentry)
            ],
            config_subentry_id=subentry_id,
        )
    if config_entry.options.get(CONF_GUEST_GROUP_ENABLED) is True:
        async_add_entities([WahaGuestGroupNotifyEntity(config_entry)])


class WahaNotifyEntity(NotifyEntity):
    """An individual WhatsApp notification destination."""

    _attr_supported_features = NotifyEntityFeature.TITLE

    def __init__(self, config_entry: WahaConfigEntry, subentry: ConfigSubentry) -> None:
        """Initialize the recipient notify entity."""
        self.config_entry = config_entry
        self._subentry = subentry
        self._attr_name = subentry.title
        self._attr_unique_id = (
            f"{config_entry.unique_id}_{subentry.unique_id or subentry.subentry_id}"
        )
        self._attr_device_info = _device_info(config_entry, subentry)
        self._attr_extra_state_attributes = contact_state_attributes(subentry.data)

    @property
    @override
    def suggested_object_id(self) -> str:
        """Use the configured contact name for the initial entity ID."""
        return self._subentry.title

    @override
    async def async_send_message(self, message: str, title: str | None = None) -> None:
        """Send a free-form notification to this contact."""
        try:
            await _async_send_to_recipient(
                self.config_entry, self._subentry, message, title
            )
        except WahaError as err:
            raise _home_assistant_error(err) from err


class WahaGuestGroupNotifyEntity(NotifyEntity):
    """One opt-in notification destination for the exact managed group."""

    _attr_supported_features = NotifyEntityFeature.TITLE
    _attr_should_poll = True

    def __init__(self, config_entry: WahaConfigEntry) -> None:
        self.config_entry = config_entry
        self._attr_name = config_entry.options.get(
            CONF_GUEST_GROUP_NAME, DEFAULT_GUEST_GROUP_NAME
        )
        self._attr_unique_id = (
            f"{config_entry.unique_id or config_entry.entry_id}_managed_guest_group"
        )

    @property
    @override
    def available(self) -> bool:
        """Do not offer group sends until identity/security/roster are confirmed."""
        return self.config_entry.runtime_data.guest_manager.snapshot()["ready"]

    @property
    @override
    def suggested_object_id(self) -> str:
        return "waha_guests"

    @override
    async def async_send_message(self, message: str, title: str | None = None) -> None:
        rendered = render_notification(message, title)
        conversation_id = None
        with suppress(Exception):
            conversation_id = self.config_entry.runtime_data.guest_registry.snapshot()[
                "conversation_id"
            ]
        try:
            await async_send_observed(
                self.config_entry,
                conversation_id,
                "group",
                {"kind": "text", "text": rendered},
                lambda: (
                    self.config_entry.runtime_data.guest_manager.async_send_to_group(
                        rendered
                    )
                ),
                origin="automation",
                content_redacted=(
                    self.config_entry.options.get(
                        "guest_group_observation_content_redacted"
                    )
                    is True
                ),
            )
        except (GuestGroupError, WahaError) as err:
            raise _home_assistant_error(err) from err


class WahaGeneralGroupNotifyEntity(NotifyEntity):
    """One explicitly configured group, with independent eligibility checks."""

    _attr_supported_features = NotifyEntityFeature.TITLE
    _attr_should_poll = True

    def __init__(self, config_entry: WahaConfigEntry, subentry: ConfigSubentry) -> None:
        self.config_entry = config_entry
        self._subentry = subentry
        self._attr_name = subentry.title
        self._attr_unique_id = (
            f"{config_entry.unique_id}_{subentry.unique_id or subentry.subentry_id}"
        )
        self._attr_device_info = _device_info(config_entry, subentry)

    @property
    @override
    def suggested_object_id(self) -> str:
        return self._subentry.title

    @property
    @override
    def available(self) -> bool:
        return (
            self.config_entry.runtime_data.general_groups.resolve_subentry(
                self._subentry.subentry_id
            )
            is not None
        )

    @override
    async def async_send_message(self, message: str, title: str | None = None) -> None:
        manager = self.config_entry.runtime_data.general_groups
        destination = manager.resolve_subentry(self._subentry.subentry_id)
        if destination is None:
            raise _home_assistant_error(GeneralGroupError("invalid_conversation"))
        rendered = render_notification(message, title)
        try:
            await async_send_observed(
                self.config_entry,
                destination.conversation_id,
                "group",
                {"kind": "text", "text": rendered},
                lambda: manager.async_send_text(destination.conversation_id, rendered),
                origin="automation",
                content_redacted=(
                    self._subentry.data.get("observation_content_redacted") is True
                ),
            )
        except (GeneralGroupError, WahaError) as err:
            raise _home_assistant_error(err) from err


async def _async_send_to_recipient(
    config_entry: WahaConfigEntry,
    subentry: ConfigSubentry,
    message: str,
    title: str | None,
) -> None:
    """Render and send one configured recipient notification."""
    rendered = render_notification(message, title)
    conversation_id = None
    try:
        registry = config_entry.runtime_data.channel_registry
        contact = await registry.async_resolve_chat(subentry.data[CONF_RECIPIENT])
        if contact is not None and contact.subentry_id == subentry.subentry_id:
            conversation_id = contact.conversation_id
    except Exception:
        # Routing observations are optional; the configured notification sends.
        pass
    await async_send_observed(
        config_entry,
        conversation_id,
        "direct",
        {"kind": "text", "text": rendered},
        lambda: config_entry.runtime_data.client.async_send_text(
            subentry.data[CONF_RECIPIENT], rendered
        ),
        origin="automation",
        content_redacted=subentry.data.get("observation_content_redacted") is True,
    )


def _device_info(config_entry: WahaConfigEntry, subentry: ConfigSubentry) -> DeviceInfo:
    """Describe the local WAHA service for one recipient subentry.

    Home Assistant 2026.8 associates a device with a single config
    subentry. Recipient entities are individual subentries, so sharing the
    old account-level identifier makes the device move between recipients
    during entity registration.
    """
    server = config_entry.runtime_data.server
    subentry_id = subentry.unique_id or subentry.subentry_id
    return DeviceInfo(
        identifiers={
            (
                DOMAIN,
                f"{config_entry.unique_id or config_entry.entry_id}:{subentry_id}",
            )
        },
        name="WAHA",
        manufacturer="WAHA",
        model=f"WhatsApp HTTP API ({server.engine or 'unknown engine'})",
        sw_version=server.version,
        entry_type=DeviceEntryType.SERVICE,
    )


def _home_assistant_error(err: Exception) -> HomeAssistantError:
    """Translate a WAHA delivery failure for Home Assistant."""
    return HomeAssistantError(
        translation_domain=DOMAIN,
        translation_key="action_failed",
        translation_placeholders={"error": str(err)},
    )
