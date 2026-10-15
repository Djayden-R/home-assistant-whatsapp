"""WAHA WhatsApp integration for Home Assistant."""

from __future__ import annotations

import logging
import re
import secrets
from dataclasses import dataclass
from datetime import timedelta
from http import HTTPStatus
from typing import cast

import voluptuous as vol
from aiohttp.web import Request, Response
from homeassistant.components import webhook
from homeassistant.config_entries import ConfigEntry, ConfigEntryState
from homeassistant.const import CONF_API_KEY, Platform
from homeassistant.core import (
    HomeAssistant,
    ServiceCall,
    ServiceResponse,
    SupportsResponse,
)
from homeassistant.exceptions import (
    ConfigEntryAuthFailed,
    ConfigEntryNotReady,
    HomeAssistantError,
    ServiceValidationError,
)
from homeassistant.helpers import (
    config_validation as cv,
)
from homeassistant.helpers import (
    entity_registry as er,
)
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.event import async_track_time_interval

from .api import (
    WahaAuthenticationError,
    WahaClient,
    WahaError,
    WahaMessage,
    WahaResponseError,
    WahaServer,
    WahaSession,
)
from .channel_registry import ChannelRegistry
from .commands import CommandRegistry, CommandRegistryError
from .const import (
    ATTR_ACTIONS,
    ATTR_CLIENT_REQUEST_ID,
    ATTR_CONTENT_REDACTED,
    ATTR_CONVERSATION_ID,
    ATTR_ENTITY_ID,
    ATTR_LINK_PREVIEW,
    ATTR_MESSAGE,
    ATTR_NO_ACTION_TITLE,
    ATTR_ORIGIN,
    ATTR_REPLY_TO_MESSAGE_ID,
    ATTR_SETTLE_SECONDS,
    ATTR_TITLE,
    ATTR_TO,
    CONF_ADDON_SLUG,
    CONF_API_URL,
    CONF_GUEST_GROUP_ENABLED,
    CONF_OBSERVATION_CONTENT_REDACTED,
    CONF_PERSON_ENTITY_ID,
    CONF_RECIPIENT,
    CONF_SESSION,
    CONF_WEBHOOK_ID,
    CONF_WEBHOOK_SECRET,
    DEFAULT_NO_ACTION_TITLE,
    DEFAULT_SETTLE_SECONDS,
    DOMAIN,
    SERVICE_GET_CHANNEL_CAPABILITIES,
    SERVICE_GET_GROUP_MEMBERSHIP,
    SERVICE_LIST_COMMANDS,
    SERVICE_REGISTER_COMMAND,
    SERVICE_SEND_MESSAGE,
    SERVICE_SEND_POLL,
    SERVICE_SEND_TO_CONVERSATION,
    SERVICE_UNREGISTER_COMMAND,
)
from .correlated import CorrelatedSendError, CorrelatedSendManager
from .general_group_inbound import WahaGeneralGroupInboundManager
from .general_groups import GeneralGroupError, GeneralGroupManager
from .guest_inbound import WahaGuestInboundManager
from .guest_manager import MIN_GOWS_VERSION, GuestGroupManager
from .guest_registry import GuestRegistry
from .helpers import normalize_recipient, render_notification
from .inbound import WahaInboundManager
from .migration import (
    legacy_group_unique_ids,
    with_webhook_credentials,
    without_obsolete_group_config,
)
from .outbound import OutboundObserver, async_send_observed
from .poll_manager import WahaPollManager
from .polls import PollValidationError, build_poll_options

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[Platform] = [Platform.NOTIFY]
CONF_CONFIG_ENTRY_ID = "config_entry_id"

CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)

SEND_MESSAGE_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_CONFIG_ENTRY_ID): cv.string,
        vol.Required(ATTR_TO): cv.string,
        vol.Required(ATTR_MESSAGE): cv.string,
        vol.Optional(ATTR_TITLE): cv.string,
        vol.Optional(ATTR_LINK_PREVIEW, default=True): cv.boolean,
    }
)

POLL_ACTION_SCHEMA = vol.Schema(
    {
        vol.Required("action"): cv.string,
        vol.Required("title"): cv.string,
    },
    extra=vol.ALLOW_EXTRA,
)

SEND_TO_CONVERSATION_SCHEMA = vol.Schema(
    {
        vol.Required(ATTR_CONVERSATION_ID): vol.All(cv.string, vol.Length(min=1)),
        vol.Required(ATTR_MESSAGE): vol.All(cv.string, vol.Length(min=1)),
        vol.Optional(ATTR_REPLY_TO_MESSAGE_ID): vol.All(cv.string, vol.Length(min=1)),
        vol.Optional(ATTR_CLIENT_REQUEST_ID): vol.All(
            cv.string, vol.Length(min=1, max=128)
        ),
        vol.Optional(ATTR_ORIGIN): vol.In(
            ("automation", "application", "human", "unknown")
        ),
        vol.Optional(ATTR_CONTENT_REDACTED, default=False): cv.boolean,
    }
)

GET_GROUP_MEMBERSHIP_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_CONFIG_ENTRY_ID): cv.string,
        vol.Optional("membership_id"): vol.All(cv.string, vol.Length(min=1)),
    }
)

REGISTER_COMMAND_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_CONFIG_ENTRY_ID): cv.string,
        vol.Required("name"): cv.string,
        vol.Optional("aliases", default=[]): vol.All(
            cv.ensure_list, [cv.string], vol.Length(max=5)
        ),
        vol.Optional("allowed_contacts", default=[]): vol.All(
            cv.ensure_list, [cv.entity_id], vol.Length(max=64)
        ),
        vol.Optional("allow_current_guests", default=False): cv.boolean,
    }
)

COMMAND_ENTRY_SCHEMA = vol.Schema({vol.Required(CONF_CONFIG_ENTRY_ID): cv.string})
UNREGISTER_COMMAND_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_CONFIG_ENTRY_ID): cv.string,
        vol.Required("name"): cv.string,
    }
)

MAX_WEBHOOK_BODY_BYTES = 256 * 1024

SEND_POLL_SCHEMA = vol.Schema(
    {
        vol.Required(ATTR_ENTITY_ID): cv.entity_id,
        vol.Required(ATTR_MESSAGE): cv.string,
        vol.Optional(ATTR_TITLE): cv.string,
        vol.Required(ATTR_ACTIONS): vol.All(cv.ensure_list, [POLL_ACTION_SCHEMA]),
        vol.Optional(ATTR_SETTLE_SECONDS, default=DEFAULT_SETTLE_SECONDS): vol.All(
            vol.Coerce(float), vol.Range(min=0, max=30)
        ),
        vol.Optional(ATTR_NO_ACTION_TITLE, default=DEFAULT_NO_ACTION_TITLE): cv.string,
        vol.Optional(ATTR_CONTENT_REDACTED, default=False): cv.boolean,
    }
)


@dataclass(slots=True)
class WahaRuntimeData:
    """Runtime data for a configured WAHA server and session."""

    client: WahaClient
    server: WahaServer
    session: WahaSession
    poll_manager: WahaPollManager
    channel_registry: ChannelRegistry
    inbound_manager: WahaInboundManager
    guest_registry: GuestRegistry
    guest_manager: GuestGroupManager
    guest_inbound_manager: WahaGuestInboundManager
    command_registry: CommandRegistry
    outbound_observer: OutboundObserver
    correlated_sends: CorrelatedSendManager
    general_groups: GeneralGroupManager
    general_group_inbound: WahaGeneralGroupInboundManager


type WahaConfigEntry = ConfigEntry[WahaRuntimeData]


async def async_setup(hass: HomeAssistant, config: dict) -> bool:
    """Set up integration-level WAHA actions."""
    hass.services.async_register(
        DOMAIN,
        SERVICE_SEND_MESSAGE,
        _async_handle_send_message,
        schema=SEND_MESSAGE_SCHEMA,
        supports_response=SupportsResponse.OPTIONAL,
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_SEND_POLL,
        _async_handle_send_poll,
        schema=SEND_POLL_SCHEMA,
        supports_response=SupportsResponse.OPTIONAL,
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_SEND_TO_CONVERSATION,
        _async_handle_send_to_conversation,
        schema=SEND_TO_CONVERSATION_SCHEMA,
        supports_response=SupportsResponse.OPTIONAL,
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_GET_GROUP_MEMBERSHIP,
        _async_handle_get_group_membership,
        schema=GET_GROUP_MEMBERSHIP_SCHEMA,
        supports_response=SupportsResponse.ONLY,
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_GET_CHANNEL_CAPABILITIES,
        _async_handle_get_channel_capabilities,
        schema=COMMAND_ENTRY_SCHEMA,
        supports_response=SupportsResponse.ONLY,
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_REGISTER_COMMAND,
        _async_handle_register_command,
        schema=REGISTER_COMMAND_SCHEMA,
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_UNREGISTER_COMMAND,
        _async_handle_unregister_command,
        schema=UNREGISTER_COMMAND_SCHEMA,
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_LIST_COMMANDS,
        _async_handle_list_commands,
        schema=COMMAND_ENTRY_SCHEMA,
        supports_response=SupportsResponse.ONLY,
    )
    return True


async def async_migrate_entry(hass: HomeAssistant, entry: WahaConfigEntry) -> bool:
    """Migrate legacy groups and add stable private webhook credentials."""
    if entry.version != 1:
        return False

    migrated_data = dict(entry.data)
    migrated_options = dict(entry.options)

    if entry.minor_version < 2:
        for subentry in tuple(entry.subentries.values()):
            migrated_subentry_data = without_obsolete_group_config(subentry.data)
            if migrated_subentry_data != subentry.data:
                hass.config_entries.async_update_subentry(
                    entry, subentry, data=migrated_subentry_data
                )

        entity_registry = er.async_get(hass)
        obsolete_unique_ids = legacy_group_unique_ids(entry.unique_id)
        for entity in er.async_entries_for_config_entry(
            entity_registry, entry.entry_id
        ):
            if (
                entity.domain == Platform.NOTIFY
                and entity.platform == DOMAIN
                and entity.unique_id in obsolete_unique_ids
            ):
                entity_registry.async_remove(entity.entity_id)

        migrated_data = without_obsolete_group_config(migrated_data)
        migrated_options = without_obsolete_group_config(migrated_options)

    if entry.minor_version < 3:
        migrated_data = with_webhook_credentials(
            migrated_data,
            secrets.token_hex(32),
            secrets.token_urlsafe(48),
        )
        hass.config_entries.async_update_entry(
            entry,
            data=migrated_data,
            options=migrated_options,
            version=1,
            minor_version=3,
        )

    return True


async def async_setup_entry(hass: HomeAssistant, entry: WahaConfigEntry) -> bool:
    """Set up a WAHA WhatsApp config entry."""
    _ensure_webhook_credentials(hass, entry)
    client = WahaClient(
        async_get_clientsession(hass),
        entry.data[CONF_API_KEY],
        entry.data[CONF_SESSION],
        base_url=entry.data[CONF_API_URL],
    )

    try:
        server = await client.async_get_server()
        session = await client.async_get_session()
    except WahaAuthenticationError as err:
        raise ConfigEntryAuthFailed("WAHA rejected the configured API key") from err
    except WahaError as err:
        raise ConfigEntryNotReady(f"Unable to connect to WAHA: {err}") from err

    channel_registry = ChannelRegistry(hass, entry, client)
    command_registry = CommandRegistry(hass, entry.entry_id)
    await command_registry.async_start()
    guest_registry = GuestRegistry(hass, entry.entry_id)
    guest_manager = GuestGroupManager(hass, entry, client, guest_registry)
    general_groups = GeneralGroupManager(
        hass, entry, client, channel_registry, guest_registry
    )
    channel_registry.set_extra_route_resolver(
        lambda conversation_id: (
            guest_registry.resolve_route(conversation_id)
            or guest_registry.resolve_group_conversation(conversation_id)
            or general_groups.conversation_destination(conversation_id)
        )
    )
    poll_manager = WahaPollManager(
        hass,
        entry,
        client,
        entry.data[CONF_WEBHOOK_SECRET],
        channel_registry,
    )
    try:
        await poll_manager.async_start()
    except Exception:
        poll_manager.stop()
        raise
    try:
        await channel_registry.async_start()
    except Exception:
        # Message correlation is an optional channel feature. A damaged or
        # incompatible channel Store must not take existing notify/polls down.
        _LOGGER.warning(
            "Could not restore WAHA channel state; using volatile state until reload"
        )
        channel_registry.use_volatile_storage()
    outbound_observer = OutboundObserver(hass, entry, channel_registry)
    correlated_sends = CorrelatedSendManager(hass, entry)
    await correlated_sends.async_start()
    general_group_inbound = WahaGeneralGroupInboundManager(
        hass, entry, general_groups, channel_registry
    )
    inbound_manager = WahaInboundManager(
        hass,
        entry,
        channel_registry,
        guest_registry,
        refresh_guest_membership=lambda: guest_manager.async_reconcile(force=True),
        command_registry=command_registry,
    )
    guest_inbound_manager = WahaGuestInboundManager(
        hass,
        entry,
        guest_registry,
        channel_registry,
        verified_bot_jid=session.account_id or "",
        refresh_membership=lambda: guest_manager.async_reconcile(force=True),
        command_registry=command_registry,
    )
    webhook_id = entry.data[CONF_WEBHOOK_ID]
    guest_events_requested = entry.options.get(CONF_GUEST_GROUP_ENABLED) is True
    guest_events_capable = guest_events_requested and _supports_guest_webhooks(server)
    guest_webhook_failure = False
    guest_callbacks_ready = False
    webhook.async_register(
        hass,
        DOMAIN,
        f"WAHA messages and poll actions ({entry.title})",
        webhook_id,
        lambda callback_hass, callback_id, request: _async_handle_waha_webhook(
            poll_manager,
            inbound_manager,
            guest_manager,
            guest_inbound_manager,
            guest_callbacks_ready,
            callback_hass,
            callback_id,
            request,
            general_group_inbound=general_group_inbound,
        ),
        local_only=True,
    )
    try:
        webhook_url = _webhook_url(hass, entry, webhook_id)
        if guest_events_capable:
            guest_events_capable = await _async_ensure_channel_webhook(
                client, webhook_url, entry.data[CONF_WEBHOOK_SECRET], group_events=True
            )
            guest_webhook_failure = not guest_events_capable
        else:
            await _async_ensure_channel_webhook(
                client, webhook_url, entry.data[CONF_WEBHOOK_SECRET]
            )
    except WahaError as err:
        webhook.async_unregister(hass, webhook_id)
        poll_manager.stop()
        channel_registry.stop()
        raise ConfigEntryNotReady(
            f"Unable to configure the private WAHA webhook: {err}"
        ) from err
    except Exception:
        webhook.async_unregister(hass, webhook_id)
        poll_manager.stop()
        channel_registry.stop()
        raise

    if guest_events_capable:
        status = await guest_manager.async_setup()
        guest_callbacks_ready = True
        if status["ready"]:
            try:
                current_session = await client.async_get_session()
            except WahaError:
                guest_manager.suspend("unverified_bot_account")
            else:
                if current_session.account_id:
                    guest_inbound_manager.update_verified_bot_jid(
                        current_session.account_id
                    )
    else:
        await guest_registry.async_start()
        guest_manager.disable_events(
            "guest_webhook_unavailable"
            if guest_webhook_failure
            else "unsupported_waha_capabilities"
            if guest_events_requested
            else "disabled"
        )

    entry.runtime_data = WahaRuntimeData(
        client,
        server,
        session,
        poll_manager,
        channel_registry,
        inbound_manager,
        guest_registry,
        guest_manager,
        guest_inbound_manager,
        command_registry,
        outbound_observer,
        correlated_sends,
        general_groups,
        general_group_inbound,
    )
    entry.async_on_unload(lambda: webhook.async_unregister(hass, webhook_id))
    entry.async_on_unload(poll_manager.stop)
    entry.async_on_unload(channel_registry.stop)
    entry.async_on_unload(entry.add_update_listener(_async_reload_entry))
    if guest_events_capable:

        async def _async_periodic_guest_refresh(_now) -> None:
            """Reconcile missed participant events without changing other channels."""
            result = await guest_manager.async_reconcile(force=True)
            if result["ready"]:
                try:
                    current_session = await client.async_get_session()
                except WahaError:
                    guest_manager.suspend("unverified_bot_account")
                else:
                    if current_session.account_id:
                        guest_inbound_manager.update_verified_bot_jid(
                            current_session.account_id
                        )

        entry.async_on_unload(
            async_track_time_interval(
                hass,
                _async_periodic_guest_refresh,
                timedelta(minutes=5),
            )
        )
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: WahaConfigEntry) -> bool:
    """Unload a WAHA WhatsApp config entry."""
    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unloaded:
        # A replacement runtime must not read or overwrite this ledger until
        # every admitted send has persisted its final or uncertain outcome.
        await entry.runtime_data.correlated_sends.async_stop()
        entry.runtime_data.channel_registry.stop()
        await entry.runtime_data.outbound_observer.async_stop()
        await entry.runtime_data.channel_registry.async_stop()
    return unloaded


async def _async_reload_entry(hass: HomeAssistant, entry: WahaConfigEntry) -> None:
    """Reload individual recipient entities after entry changes."""
    await hass.config_entries.async_reload(entry.entry_id)


async def _async_handle_send_message(call: ServiceCall) -> ServiceResponse:
    """Send one free-form message to an arbitrary phone number."""
    entry = _loaded_entry(call.hass, call.data[CONF_CONFIG_ENTRY_ID])
    try:
        recipient = normalize_recipient(call.data[ATTR_TO])
        result = await entry.runtime_data.client.async_send_text(
            recipient,
            render_notification(call.data[ATTR_MESSAGE], call.data.get(ATTR_TITLE)),
            link_preview=call.data[ATTR_LINK_PREVIEW],
        )
        await _async_remember_outbound(entry, result)
    except ValueError as err:
        raise ServiceValidationError(
            translation_domain=DOMAIN,
            translation_key="invalid_recipient",
            translation_placeholders={"error": str(err)},
        ) from err
    except WahaError as err:
        raise HomeAssistantError(
            translation_domain=DOMAIN,
            translation_key="action_failed",
            translation_placeholders={"error": str(err)},
        ) from err

    if call.return_response:
        return _service_response(result)
    return None


async def _async_handle_send_poll(call: ServiceCall) -> ServiceResponse:
    """Send an actionable poll to one configured individual contact."""
    entry, recipient, person_entity_id = _configured_recipient(
        call.hass, call.data[ATTR_ENTITY_ID]
    )
    try:
        options = build_poll_options(
            call.data[ATTR_ACTIONS], call.data[ATTR_NO_ACTION_TITLE]
        )
        question = _poll_question(call.data[ATTR_MESSAGE], call.data.get(ATTR_TITLE))
        option_titles = [option.title for option in options]
        registry = entry.runtime_data.channel_registry
        try:
            contact = await registry.async_resolve_chat(recipient)
        except Exception:
            # Observation routing is optional and must not block a poll send.
            contact = None
        result = await async_send_observed(
            entry,
            contact.conversation_id if contact is not None else None,
            "direct",
            {"kind": "poll", "text": question, "options": option_titles},
            lambda: entry.runtime_data.client.async_send_poll(
                recipient, question, option_titles
            ),
            origin="automation",
            content_redacted=(
                call.data.get(ATTR_CONTENT_REDACTED, False)
                or _contact_content_redacted(entry, contact)
            ),
        )
        if result.id is None:
            raise WahaResponseError("WAHA did not return the poll message ID")
        await entry.runtime_data.poll_manager.async_register_poll(
            result.id,
            result.chat_id,
            options,
            call.data[ATTR_SETTLE_SECONDS],
            person_entity_id,
        )
    except PollValidationError as err:
        raise ServiceValidationError(
            translation_domain=DOMAIN,
            translation_key="invalid_poll",
            translation_placeholders={"error": str(err)},
        ) from err
    except WahaError as err:
        raise HomeAssistantError(
            translation_domain=DOMAIN,
            translation_key="action_failed",
            translation_placeholders={"error": str(err)},
        ) from err

    if call.return_response:
        return _service_response(result)
    return None


async def _async_handle_send_to_conversation(call: ServiceCall) -> ServiceResponse:
    """Send text through an eligible route, optionally recording durable intent."""
    conversation_id = call.data[ATTR_CONVERSATION_ID]
    request_id = call.data.get(ATTR_CLIENT_REQUEST_ID)
    entry = None
    for candidate in call.hass.config_entries.async_entries(DOMAIN):
        if candidate.state is not ConfigEntryState.LOADED:
            continue
        runtime = candidate.runtime_data
        if (
            runtime.channel_registry.resolve_conversation(conversation_id) is not None
            or runtime.guest_registry.resolve_group_conversation(conversation_id)
            or runtime.guest_registry.resolve_route(conversation_id)
            or runtime.general_groups.resolve_conversation(conversation_id) is not None
            or (
                request_id is not None
                and runtime.correlated_sends.owns_request(conversation_id, request_id)
            )
        ):
            entry = candidate
            break

    if entry is None:
        if request_id is not None:
            return {
                ATTR_CONVERSATION_ID: conversation_id,
                ATTR_CLIENT_REQUEST_ID: request_id,
                "message_id": None,
                "status": "failed",
                "error_code": "invalid_conversation",
                "replayed": False,
            }
        raise ServiceValidationError(
            translation_domain=DOMAIN, translation_key="invalid_conversation"
        )

    runtime = entry.runtime_data
    registry = runtime.channel_registry
    quote_token = call.data.get(ATTR_REPLY_TO_MESSAGE_ID)
    origin = call.data.get(ATTR_ORIGIN, "unknown")
    redacted = call.data.get(ATTR_CONTENT_REDACTED, False)

    async def _send() -> dict:
        # Replayed requests return before this preflight: an expired quote must
        # never cause a second submission or invalidate a saved acceptance.
        contact = registry.resolve_conversation(conversation_id)
        group_destination = runtime.guest_registry.resolve_group_conversation(
            conversation_id
        )
        guest_destination = runtime.guest_registry.resolve_route(conversation_id)
        general_destination = runtime.general_groups.resolve_conversation(
            conversation_id
        )
        quoted_id = None
        if quote_token is not None:
            quoted_id = registry.resolve_message(quote_token, conversation_id)
            if quoted_id is None:
                raise CorrelatedSendError("invalid_reply_target")

        conversation_type = (
            "group"
            if group_destination or general_destination is not None
            else "direct"
        )

        async def _transmit() -> WahaMessage:
            if contact is not None:
                return await runtime.client.async_send_text(
                    contact.recipient,
                    call.data[ATTR_MESSAGE],
                    reply_to_message_id=quoted_id,
                )
            if general_destination is not None:
                try:
                    return await runtime.general_groups.async_send_text(
                        conversation_id, call.data[ATTR_MESSAGE], reply_to=quoted_id
                    )
                except GeneralGroupError as err:
                    raise CorrelatedSendError("invalid_conversation") from err
            if group_destination is not None:
                if not await runtime.guest_manager.async_verify_group_destination(
                    group_destination
                ):
                    raise CorrelatedSendError("invalid_conversation")
                return await runtime.client.async_send_group_text(
                    group_destination,
                    call.data[ATTR_MESSAGE],
                    reply_to_message_id=quoted_id,
                )
            if guest_destination is not None:

                async def _send_guest(destination: str) -> WahaMessage:
                    return await runtime.client.async_send_text(
                        destination,
                        call.data[ATTR_MESSAGE],
                        reply_to_message_id=quoted_id,
                    )

                try:
                    return await runtime.guest_registry.async_with_route(
                        conversation_id,
                        runtime.guest_manager.async_verify_guest_destination,
                        _send_guest,
                    )
                except ValueError as err:
                    raise CorrelatedSendError("invalid_conversation") from err
            raise CorrelatedSendError("invalid_conversation")

        route_redacted = _contact_content_redacted(entry, contact)
        if group_destination is not None:
            route_redacted = bool(
                entry.options.get("guest_group_observation_content_redacted")
            )
        if general_destination is not None:
            subentry = entry.subentries.get(general_destination.subentry_id)
            route_redacted = bool(
                subentry and subentry.data.get(CONF_OBSERVATION_CONTENT_REDACTED)
            )
        result = await async_send_observed(
            entry,
            conversation_id,
            conversation_type,
            {
                "kind": "text",
                "text": call.data[ATTR_MESSAGE],
                "in_reply_to": quote_token,
            },
            _transmit,
            origin=origin,
            client_request_id=request_id,
            content_redacted=redacted or route_redacted,
        )
        message_id = None
        if result.id is not None:
            try:
                token = registry.message_token(result.id)
                if registry.resolve_message(token, conversation_id) is not None:
                    message_id = token
            except Exception:
                # Known acceptance survives optional correlation failure.
                _LOGGER.warning("WAHA text accepted without message tracking")
        return {
            "status": result.outcome,
            "message_id": message_id,
            **(
                {"error_code": "outcome_unknown"} if result.outcome == "unknown" else {}
            ),
        }

    if request_id is not None:
        return await runtime.correlated_sends.async_execute(
            conversation_id,
            request_id,
            {
                "message": call.data[ATTR_MESSAGE],
                "reply_to_message_id": quote_token,
                "origin": origin,
                "content_redacted": redacted,
            },
            _send,
        )

    try:
        result = await _send()
    except CorrelatedSendError as err:
        raise ServiceValidationError(
            translation_domain=DOMAIN, translation_key=err.code
        ) from err
    except WahaError as err:
        raise HomeAssistantError(
            translation_domain=DOMAIN,
            translation_key="action_failed",
            translation_placeholders={"error": str(err)},
        ) from err
    if call.return_response:
        response = {ATTR_CONVERSATION_ID: conversation_id}
        if result["message_id"] is not None:
            response["message_id"] = result["message_id"]
        return response
    return None


def _contact_content_redacted(entry: WahaConfigEntry, contact) -> bool:
    """Read an explicit destination policy; never infer sensitivity from text."""
    if contact is None:
        return False
    subentry = entry.subentries.get(contact.subentry_id)
    return bool(subentry and subentry.data.get(CONF_OBSERVATION_CONTENT_REDACTED))


async def _async_handle_get_group_membership(call: ServiceCall) -> ServiceResponse:
    """Return a phone-free, explicit current/unknown group membership snapshot."""
    entry = _loaded_entry(call.hass, call.data[CONF_CONFIG_ENTRY_ID])
    guest_manager = entry.runtime_data.guest_manager
    # This query may drive access automations, so a cached roster is not
    # sufficient proof of current membership. Reconcile read-only and return
    # ready=false if WAHA or the group safeguards cannot be verified.
    await guest_manager.async_reconcile(force=True)
    snapshot = entry.runtime_data.guest_registry.snapshot(
        membership_id=call.data.get("membership_id")
    )
    snapshot["failure_reason"] = guest_manager.failure_reason
    return snapshot


async def _async_handle_get_channel_capabilities(call: ServiceCall) -> ServiceResponse:
    """Describe this active connection without exposing private transport state."""
    entry = _loaded_entry(call.hass, call.data[CONF_CONFIG_ENTRY_ID])
    runtime = entry.runtime_data
    observation_enabled = False
    observation_health = "unavailable"
    try:
        observation_enabled = runtime.outbound_observer.enabled
        observation_health = runtime.outbound_observer.health
    except Exception:
        # Report missing observation instead of making capability discovery fail.
        pass
    try:
        server = await runtime.client.async_get_server()
        session = await runtime.client.async_get_session()
        connected = session.status == "WORKING" and bool(session.account_id)
    except WahaError:
        server = runtime.server
        connected = False

    groups_ready = False
    if connected and _supports_guest_webhooks(server):
        for subentry_id in entry.subentries:
            destination = runtime.general_groups.resolve_subentry(subentry_id)
            if destination is None:
                continue
            try:
                await runtime.general_groups.async_verify(destination)
            except GeneralGroupError, WahaError:
                continue
            groups_ready = True
            break

    capabilities = {
        "schema_versions": [1],
        "outbound_observations": (connected and observation_enabled),
        "correlated_sends": connected
        and runtime.correlated_sends.persistence_available,
        "general_groups": groups_ready,
        "mentions": groups_ready,
        "delivery_receipts": False,
        "message_changes": False,
        "media_retrieval": False,
        "transport_replay": False,
        "manual_outbound_observations": False,
        "observation_health": (observation_health if connected else "unavailable"),
    }
    capabilities.update(runtime.correlated_sends.capabilities)
    capabilities["correlated_sends"] = (
        connected and runtime.correlated_sends.persistence_available
    )
    return capabilities


async def _async_require_command_admin(call: ServiceCall) -> None:
    """Require an active HA administrator for persistent command changes."""
    user_id = call.context.user_id
    user = await call.hass.auth.async_get_user(user_id) if user_id else None
    if user is None or not user.is_active or not user.is_admin:
        raise ServiceValidationError(
            "Only an active Home Assistant administrator can manage WhatsApp commands"
        )


async def _async_handle_register_command(call: ServiceCall) -> None:
    """Register an inert command selector, never an executable HA action."""
    await _async_require_command_admin(call)
    entry = _loaded_entry(call.hass, call.data[CONF_CONFIG_ENTRY_ID])
    contact_ids: list[str] = []
    for entity_id in call.data["allowed_contacts"]:
        contact_entry, _, _ = _configured_recipient(call.hass, entity_id)
        if contact_entry.entry_id != entry.entry_id:
            raise ServiceValidationError(
                "Allowed contact belongs to another WAHA entry"
            )
        entity = er.async_get(call.hass).async_get(entity_id)
        if entity is None or entity.config_subentry_id is None:
            raise ServiceValidationError("Allowed contact is unavailable")
        contact_ids.append(entity.config_subentry_id)
    try:
        await entry.runtime_data.command_registry.async_register(
            call.data["name"],
            call.data["aliases"],
            contact_ids,
            call.data["allow_current_guests"],
        )
    except CommandRegistryError as err:
        raise ServiceValidationError(str(err)) from err


async def _async_handle_unregister_command(call: ServiceCall) -> None:
    """Remove a registered command without changing its consumer automations."""
    await _async_require_command_admin(call)
    entry = _loaded_entry(call.hass, call.data[CONF_CONFIG_ENTRY_ID])
    try:
        await entry.runtime_data.command_registry.async_unregister(call.data["name"])
    except CommandRegistryError as err:
        raise ServiceValidationError(str(err)) from err


async def _async_handle_list_commands(call: ServiceCall) -> ServiceResponse:
    """List one entry's non-sensitive command declarations."""
    await _async_require_command_admin(call)
    entry = _loaded_entry(call.hass, call.data[CONF_CONFIG_ENTRY_ID])
    try:
        return {"commands": entry.runtime_data.command_registry.list_commands()}
    except CommandRegistryError as err:
        raise ServiceValidationError(str(err)) from err


async def _async_remember_outbound(entry: WahaConfigEntry, result: WahaMessage) -> None:
    """Track configured-contact sends for later reactions and quoted replies."""
    if result.id is None:
        return
    registry = entry.runtime_data.channel_registry
    try:
        contact = await registry.async_resolve_chat(result.chat_id)
        if contact is not None:
            await registry.async_remember_message(result.id, contact.conversation_id)
    except Exception:
        # Tracking is optional after a successful send and must not change the
        # legacy notification or poll action's delivery result.
        _LOGGER.warning("WAHA sent a message but channel tracking failed")


def _loaded_entry(hass: HomeAssistant, entry_id: str) -> WahaConfigEntry:
    """Resolve and validate the config entry selected by an action."""
    entry = hass.config_entries.async_get_entry(entry_id)
    if entry is None or entry.domain != DOMAIN:
        raise ServiceValidationError(
            translation_domain=DOMAIN,
            translation_key="invalid_config_entry",
            translation_placeholders={"entry_id": entry_id},
        )
    if entry.state is not ConfigEntryState.LOADED:
        raise ServiceValidationError(
            translation_domain=DOMAIN,
            translation_key="entry_not_loaded",
            translation_placeholders={"entry_title": entry.title},
        )
    return cast(WahaConfigEntry, entry)


def _configured_recipient(
    hass: HomeAssistant, entity_id: str
) -> tuple[WahaConfigEntry, str, str | None]:
    """Resolve an integration-owned notify entity to its private recipient."""
    registry_entry = er.async_get(hass).async_get(entity_id)
    if (
        registry_entry is None
        or registry_entry.domain != Platform.NOTIFY
        or registry_entry.platform != DOMAIN
        or registry_entry.config_entry_id is None
        or registry_entry.config_subentry_id is None
    ):
        raise ServiceValidationError(
            translation_domain=DOMAIN,
            translation_key="invalid_notify_entity",
            translation_placeholders={"entity_id": entity_id},
        )
    entry = _loaded_entry(hass, registry_entry.config_entry_id)
    subentry = entry.subentries.get(registry_entry.config_subentry_id)
    if subentry is None or CONF_RECIPIENT not in subentry.data:
        raise ServiceValidationError(
            translation_domain=DOMAIN,
            translation_key="invalid_notify_entity",
            translation_placeholders={"entity_id": entity_id},
        )
    person_entity_id = subentry.data.get(CONF_PERSON_ENTITY_ID)
    return (
        entry,
        subentry.data[CONF_RECIPIENT],
        person_entity_id if isinstance(person_entity_id, str) else None,
    )


def _poll_question(message: str, title: str | None) -> str:
    """Render a poll question without WhatsApp markdown side effects."""
    return f"{title.strip()}\n\n{message}" if title and title.strip() else message


def _ensure_webhook_credentials(hass: HomeAssistant, entry: WahaConfigEntry) -> None:
    """Repair entries missing credentials without rotating valid callbacks."""
    migrated = with_webhook_credentials(
        entry.data,
        secrets.token_hex(32),
        secrets.token_urlsafe(48),
    )
    if migrated != entry.data:
        hass.config_entries.async_update_entry(entry, data=migrated)


def _webhook_url(hass: HomeAssistant, entry: WahaConfigEntry, webhook_id: str) -> str:
    """Build the private callback URL reachable from the configured WAHA host."""
    if CONF_ADDON_SLUG in entry.data:
        return f"http://homeassistant:8123{webhook.async_generate_path(webhook_id)}"
    return webhook.async_generate_url(
        hass,
        webhook_id,
        allow_external=False,
        prefer_external=False,
    )


def _supports_guest_webhooks(server: WahaServer) -> bool:
    """Keep older external WAHA servers' direct/poll webhooks unchanged."""
    if not isinstance(server.engine, str) or server.engine.upper() != "GOWS":
        return False
    match = re.fullmatch(
        r"(\d{4})\.(\d{1,2})\.(\d+)(?:\+[0-9A-Za-z.-]+)?", server.version
    )
    return bool(match and tuple(map(int, match.groups())) >= MIN_GOWS_VERSION)


async def _async_ensure_channel_webhook(
    client: WahaClient,
    url: str,
    hmac_key: str,
    *,
    group_events: bool = False,
) -> bool:
    """Subscribe optional group events without disrupting the existing channel."""
    if group_events:
        try:
            await client.async_ensure_webhook(url, hmac_key, group_events=True)
        except WahaError:
            _LOGGER.warning(
                "WAHA guest webhook events were unavailable; "
                "individual notifications and polls remain enabled"
            )
        else:
            return True
    await client.async_ensure_webhook(url, hmac_key)
    return False


async def _async_handle_waha_webhook(
    manager: WahaPollManager,
    inbound_manager: WahaInboundManager,
    guest_manager: GuestGroupManager,
    guest_inbound_manager: WahaGuestInboundManager,
    guest_callbacks_ready: bool,
    hass: HomeAssistant,
    webhook_id: str,
    request: Request,
    *,
    general_group_inbound: WahaGeneralGroupInboundManager | None = None,
) -> Response:
    """Authenticate and route one internal WAHA event."""
    del hass, webhook_id
    raw_body = await request.content.read(MAX_WEBHOOK_BODY_BYTES + 1)
    if len(raw_body) > MAX_WEBHOOK_BODY_BYTES:
        return Response(status=HTTPStatus.REQUEST_ENTITY_TOO_LARGE)
    algorithm = request.headers.get("X-Webhook-Hmac-Algorithm", "").lower()
    signature = request.headers.get("X-Webhook-Hmac")
    if algorithm != "sha512" or not manager.verify_signature(raw_body, signature):
        return Response(status=HTTPStatus.UNAUTHORIZED)
    payload = manager.decode_payload(raw_body)
    if payload is None:
        return Response(status=HTTPStatus.BAD_REQUEST)
    event_name = payload.get("event")
    if event_name in ("poll.vote", "poll.vote.failed"):
        await manager.async_handle_payload(payload)
    elif isinstance(event_name, str) and event_name.startswith("group.v2."):
        if guest_callbacks_ready:
            await guest_manager.async_handle_webhook(payload)
    elif event_name in ("message", "message.reaction"):
        if general_group_inbound is not None and (
            await general_group_inbound.async_handle_payload(payload)
        ):
            return Response(status=HTTPStatus.OK)
        if not guest_callbacks_ready or not (
            await guest_inbound_manager.async_handle_payload(payload)
        ):
            await inbound_manager.async_handle_payload(payload)
    return Response(status=HTTPStatus.OK)


def _service_response(result: WahaMessage) -> dict[str, str]:
    """Format an action response for Home Assistant."""
    response = {"chat_id": result.chat_id}
    if result.id is not None:
        response["message_id"] = result.id
    return response
