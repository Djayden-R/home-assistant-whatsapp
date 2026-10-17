"""Config flow for WAHA WhatsApp."""

from __future__ import annotations

import secrets
from collections.abc import Mapping
from typing import Any, override

import voluptuous as vol
from homeassistant.config_entries import (
    ConfigEntryState,
    ConfigFlow,
    ConfigFlowResult,
    ConfigSubentry,
    ConfigSubentryFlow,
    OptionsFlow,
    SubentryFlowResult,
)
from homeassistant.const import CONF_API_KEY, CONF_NAME
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.selector import (
    BooleanSelector,
    EntityFilterSelectorConfig,
    EntitySelector,
    EntitySelectorConfig,
    SelectSelector,
    SelectSelectorConfig,
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
)
from homeassistant.helpers.service_info.hassio import HassioServiceInfo

from . import WahaConfigEntry
from .api import (
    WahaAuthenticationError,
    WahaClient,
    WahaConnectionError,
    WahaError,
    WahaServer,
    WahaSession,
    WahaSessionNotFoundError,
)
from .const import (
    CONF_ADDON_SLUG,
    CONF_API_URL,
    CONF_GUEST_GROUP_ENABLED,
    CONF_GUEST_GROUP_EVER_ENABLED,
    CONF_GUEST_GROUP_EXCLUDED_ADMIN_USER_IDS,
    CONF_GUEST_GROUP_NAME,
    CONF_GUEST_GROUP_REVIEWED_ADMIN_FINGERPRINT,
    CONF_PERSON_ENTITY_ID,
    CONF_RECIPIENT,
    CONF_SESSION,
    CONF_WEBHOOK_ID,
    CONF_WEBHOOK_SECRET,
    DEFAULT_API_URL,
    DEFAULT_GUEST_GROUP_NAME,
    DEFAULT_SESSION,
    DOMAIN,
    SUBENTRY_TYPE_RECIPIENT,
)
from .general_groups import (
    CONF_GROUP_ACCOUNT,
    CONF_GROUP_ENABLED,
    CONF_GROUP_ID,
    SUBENTRY_TYPE_GROUP,
    GeneralGroupError,
    group_id_matches,
    normalize_group_id,
)
from .guest_admins import (
    GuestAdminPreview,
    async_guest_admin_preview,
    reviewed_admin_fingerprint,
)
from .guest_manager import GuestGroupError, GuestGroupManager
from .guest_registry import GuestRegistry
from .helpers import normalize_recipient


def _account_schema(defaults: Mapping[str, Any] | None = None) -> vol.Schema:
    """Build the manual/reconfigure connection form."""
    values = defaults or {}
    return vol.Schema(
        {
            vol.Required(
                CONF_API_URL, default=values.get(CONF_API_URL, DEFAULT_API_URL)
            ): TextSelector(TextSelectorConfig(type=TextSelectorType.URL)),
            vol.Required(CONF_API_KEY, default=values.get(CONF_API_KEY, "")): (
                TextSelector(
                    TextSelectorConfig(
                        type=TextSelectorType.PASSWORD,
                        autocomplete="current-password",
                    )
                )
            ),
            vol.Required(
                CONF_SESSION, default=values.get(CONF_SESSION, DEFAULT_SESSION)
            ): TextSelector(TextSelectorConfig()),
        }
    )


REAUTH_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_API_KEY): TextSelector(
            TextSelectorConfig(
                type=TextSelectorType.PASSWORD,
                autocomplete="current-password",
            )
        )
    }
)

RECIPIENT_SCHEMA = vol.Schema(
    {
        vol.Optional(CONF_NAME): TextSelector(TextSelectorConfig()),
        vol.Optional(CONF_PERSON_ENTITY_ID): EntitySelector(
            EntitySelectorConfig(filter=EntityFilterSelectorConfig(domain="person"))
        ),
        vol.Required(CONF_RECIPIENT): TextSelector(
            TextSelectorConfig(type=TextSelectorType.TEL)
        ),
        vol.Optional("observation_content_redacted", default=False): BooleanSelector(),
    }
)


class WahaWhatsAppConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle configuration for a WAHA server and session."""

    VERSION = 1
    MINOR_VERSION = 3

    @staticmethod
    @callback
    def async_get_options_flow(
        config_entry: WahaConfigEntry,
    ) -> WahaWhatsAppOptionsFlow:
        """Offer opt-in guest-group configuration for an existing entry."""
        return WahaWhatsAppOptionsFlow()

    def __init__(self) -> None:
        """Initialize discovery state."""
        self._discovery_data: dict[str, Any] | None = None
        self._discovery_unique_id: str | None = None

    @classmethod
    @callback
    @override
    def async_get_supported_subentry_types(
        cls, config_entry: WahaConfigEntry
    ) -> dict[str, type[ConfigSubentryFlow]]:
        """Return the recipient subentries supported by this integration."""
        return {
            SUBENTRY_TYPE_RECIPIENT: RecipientSubentryFlow,
            SUBENTRY_TYPE_GROUP: GeneralGroupSubentryFlow,
        }

    @override
    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Manually connect to a WAHA server."""
        errors: dict[str, str] = {}
        placeholders: dict[str, str] = {}

        if user_input is not None:
            data = _normalize_account_data(user_input)
            try:
                server, session = await _validate_account(self.hass, data)
            except WahaAuthenticationError:
                errors["base"] = "invalid_auth"
            except WahaConnectionError:
                errors["base"] = "cannot_connect"
            except WahaSessionNotFoundError as err:
                errors["base"] = "session_not_found"
                placeholders["error"] = str(err)
            except WahaError as err:
                errors["base"] = "waha_error"
                placeholders["error"] = str(err)
            else:
                unique_id = _manual_unique_id(data)
                await self.async_set_unique_id(unique_id)
                self._abort_if_unique_id_configured()
                return self.async_create_entry(
                    title=_entry_title(server, session),
                    data=_with_new_webhook_credentials(data),
                )

        return self.async_show_form(
            step_id="user",
            data_schema=self.add_suggested_values_to_schema(
                _account_schema(), user_input or {}
            ),
            errors=errors,
            description_placeholders=placeholders,
        )

    @override
    async def async_step_hassio(
        self, discovery_info: HassioServiceInfo
    ) -> ConfigFlowResult:
        """Handle automatic discovery from the companion HAOS app."""
        try:
            data = _account_data_from_discovery(discovery_info)
        except KeyError, TypeError, ValueError:
            return self.async_abort(reason="invalid_discovery")

        self._discovery_data = data
        self._discovery_unique_id = f"addon:{discovery_info.slug}:{data[CONF_SESSION]}"
        await self.async_set_unique_id(self._discovery_unique_id)
        self._abort_if_unique_id_configured(updates=data, reload_on_update=False)
        self.context["title_placeholders"] = {"name": discovery_info.name}
        return await self.async_step_hassio_confirm()

    async def async_step_hassio_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Confirm and validate the automatically discovered app."""
        if self._discovery_data is None or self._discovery_unique_id is None:
            return self.async_abort(reason="invalid_discovery")

        errors: dict[str, str] = {}
        placeholders: dict[str, str] = {"session": self._discovery_data[CONF_SESSION]}
        if user_input is not None:
            try:
                server, session = await _validate_account(
                    self.hass, self._discovery_data
                )
            except WahaAuthenticationError:
                errors["base"] = "invalid_auth"
            except WahaConnectionError:
                errors["base"] = "cannot_connect"
            except WahaSessionNotFoundError as err:
                errors["base"] = "session_not_found"
                placeholders["error"] = str(err)
            except WahaError as err:
                errors["base"] = "waha_error"
                placeholders["error"] = str(err)
            else:
                return self.async_create_entry(
                    title=_entry_title(server, session),
                    data=_with_new_webhook_credentials(self._discovery_data),
                )

        return self.async_show_form(
            step_id="hassio_confirm",
            data_schema=vol.Schema({}),
            errors=errors,
            description_placeholders=placeholders,
        )

    @override
    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Update the WAHA URL, API key, or session."""
        entry = self._get_reconfigure_entry()
        errors: dict[str, str] = {}
        placeholders: dict[str, str] = {}

        if user_input is not None:
            data = _normalize_account_data(user_input)
            if CONF_ADDON_SLUG in entry.data:
                data[CONF_ADDON_SLUG] = entry.data[CONF_ADDON_SLUG]
            for key in (CONF_WEBHOOK_ID, CONF_WEBHOOK_SECRET):
                if key in entry.data:
                    data[key] = entry.data[key]
            try:
                server, session = await _validate_account(self.hass, data)
            except WahaAuthenticationError:
                errors["base"] = "invalid_auth"
            except WahaConnectionError:
                errors["base"] = "cannot_connect"
            except WahaSessionNotFoundError as err:
                errors["base"] = "session_not_found"
                placeholders["error"] = str(err)
            except WahaError as err:
                errors["base"] = "waha_error"
                placeholders["error"] = str(err)
            else:
                unique_id = entry.unique_id
                if CONF_ADDON_SLUG not in entry.data:
                    unique_id = _manual_unique_id(data)
                return self.async_update_and_abort(
                    entry,
                    unique_id=unique_id,
                    title=_entry_title(server, session),
                    data=data,
                )

        return self.async_show_form(
            step_id="reconfigure",
            data_schema=self.add_suggested_values_to_schema(
                _account_schema(entry.data), user_input or entry.data
            ),
            errors=errors,
            description_placeholders=placeholders,
        )

    async def async_step_reauth(
        self, entry_data: Mapping[str, Any]
    ) -> ConfigFlowResult:
        """Start reauthentication."""
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Replace a changed WAHA API key."""
        entry = self._get_reauth_entry()
        errors: dict[str, str] = {}

        if user_input is not None:
            updated_data = {**entry.data, CONF_API_KEY: user_input[CONF_API_KEY]}
            try:
                server, session = await _validate_account(self.hass, updated_data)
            except WahaAuthenticationError:
                errors["base"] = "invalid_auth"
            except WahaConnectionError:
                errors["base"] = "cannot_connect"
            except WahaError:
                errors["base"] = "waha_error"
            else:
                return self.async_update_and_abort(
                    entry,
                    title=_entry_title(server, session),
                    data=updated_data,
                    reason="reauth_successful",
                )

        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=REAUTH_SCHEMA,
            errors=errors,
        )


class WahaWhatsAppOptionsFlow(OptionsFlow):
    """Review guest-group eligibility before recording an opt-in request."""

    def __init__(self) -> None:
        """Keep the reviewed proposal only for this options-flow instance."""
        self._proposal: dict[str, Any] | None = None
        self._reviewed_contacts: tuple[tuple[str, str], ...] = ()
        self._reviewed_gaps: tuple[tuple[str, str], ...] = ()
        self._reviewed_fingerprint: str | None = None
        self._recovery_group_id: str | None = None
        self._recovery_bot_account: str | None = None

    def _recovery_manager(self) -> GuestGroupManager | None:
        runtime = getattr(self.config_entry, "runtime_data", None)
        manager = getattr(runtime, "guest_manager", None)
        return manager if isinstance(manager, GuestGroupManager) else None

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Show mapped hosts and require a decision for each unmapped admin."""
        preview = await async_guest_admin_preview(self.hass, self.config_entry)
        options = self.config_entry.options
        errors: dict[str, str] = {}
        if user_input is not None:
            previously_enabled = (
                options.get(CONF_GUEST_GROUP_EVER_ENABLED) is True
                or options.get(CONF_GUEST_GROUP_ENABLED) is True
                or CONF_GUEST_GROUP_REVIEWED_ADMIN_FINGERPRINT in options
            )
            enabled = user_input.get(CONF_GUEST_GROUP_ENABLED) is True
            name = str(user_input.get(CONF_GUEST_GROUP_NAME, "")).strip()
            raw_excluded = user_input.get(CONF_GUEST_GROUP_EXCLUDED_ADMIN_USER_IDS, [])
            gap_ids = {gap.user_id for gap in preview.gaps}
            if not isinstance(raw_excluded, list) or not all(
                isinstance(item, str) for item in raw_excluded
            ):
                errors["base"] = "invalid_exclusions"
            else:
                excluded = set(raw_excluded)
                if len(excluded) != len(raw_excluded) or not excluded <= gap_ids:
                    errors["base"] = "invalid_exclusions"
                elif enabled and (not name or len(name) > 100):
                    errors[CONF_GUEST_GROUP_NAME] = "invalid_group_name"
                elif enabled and not preview.contacts:
                    errors["base"] = "no_mapped_admin"
                elif enabled and excluded != gap_ids:
                    errors["base"] = "unresolved_admins"
                else:
                    proposal = {
                        "media_enabled": user_input.get("media_enabled") is True,
                        CONF_GUEST_GROUP_ENABLED: enabled,
                        CONF_GUEST_GROUP_NAME: name or DEFAULT_GUEST_GROUP_NAME,
                        CONF_GUEST_GROUP_EXCLUDED_ADMIN_USER_IDS: sorted(excluded),
                        "outbound_observations_enabled": (
                            user_input.get("outbound_observations_enabled") is True
                        ),
                        "guest_group_observation_content_redacted": (
                            user_input.get("guest_group_observation_content_redacted")
                            is True
                        ),
                    }
                    if not enabled:
                        return self.async_create_entry(
                            title="",
                            data={
                                **options,
                                **proposal,
                                CONF_GUEST_GROUP_EVER_ENABLED: previously_enabled,
                            },
                        )
                    self._proposal = proposal
                    self._reviewed_contacts = tuple(
                        (contact.user_id, contact.chat_id)
                        for contact in preview.contacts
                    )
                    self._reviewed_gaps = tuple(
                        (gap.user_id, gap.reason) for gap in preview.gaps
                    )
                    self._reviewed_fingerprint = reviewed_admin_fingerprint(
                        preview,
                        sorted(excluded),
                        self.config_entry.data[CONF_WEBHOOK_SECRET],
                    )
                    manager = self._recovery_manager()
                    if (
                        manager is not None
                        and manager.registry.provisioning_status
                        in ("creating", "create_unknown")
                        and options.get(CONF_GUEST_GROUP_REVIEWED_ADMIN_FINGERPRINT)
                        == self._reviewed_fingerprint
                    ):
                        return await self.async_step_recover()
                    return await self.async_step_confirm()

        return self.async_show_form(
            step_id="init",
            data_schema=self.add_suggested_values_to_schema(
                _guest_group_options_schema(preview, options),
                user_input or options,
            ),
            errors=errors,
            description_placeholders=_guest_admin_placeholders(preview),
        )

    async def async_step_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Require a separate submit after a fresh eligibility check."""
        if self._proposal is None:
            return await self.async_step_init()
        preview = await async_guest_admin_preview(self.hass, self.config_entry)
        current_contacts = tuple(
            (contact.user_id, contact.chat_id) for contact in preview.contacts
        )
        current_gaps = tuple((gap.user_id, gap.reason) for gap in preview.gaps)
        gap_ids = {gap.user_id for gap in preview.gaps}
        excluded = set(self._proposal[CONF_GUEST_GROUP_EXCLUDED_ADMIN_USER_IDS])
        current_fingerprint = reviewed_admin_fingerprint(
            preview,
            sorted(excluded),
            self.config_entry.data[CONF_WEBHOOK_SECRET],
        )
        if (
            current_contacts != self._reviewed_contacts
            or current_gaps != self._reviewed_gaps
            or current_fingerprint != self._reviewed_fingerprint
            or not current_contacts
            or gap_ids != excluded
        ):
            self._proposal = None
            return self.async_show_form(
                step_id="init",
                data_schema=_guest_group_options_schema(
                    preview, self.config_entry.options
                ),
                errors={"base": "admin_mapping_changed"},
                description_placeholders=_guest_admin_placeholders(preview),
            )
        if user_input is not None:
            options = self.config_entry.options
            previously_enabled = (
                options.get(CONF_GUEST_GROUP_EVER_ENABLED) is True
                or options.get(CONF_GUEST_GROUP_ENABLED) is True
                or CONF_GUEST_GROUP_REVIEWED_ADMIN_FINGERPRINT in options
            )
            if not previously_enabled:
                try:
                    await GuestRegistry(
                        self.hass, self.config_entry.entry_id
                    ).async_initialize_empty()
                except Exception:
                    return self.async_show_form(
                        step_id="confirm",
                        data_schema=vol.Schema({}),
                        errors={"base": "guest_registry_unavailable"},
                        description_placeholders={
                            **_guest_admin_placeholders(preview),
                            "group_name": self._proposal[CONF_GUEST_GROUP_NAME],
                        },
                    )
            return self.async_create_entry(
                title="",
                data={
                    **self.config_entry.options,
                    **self._proposal,
                    CONF_GUEST_GROUP_EVER_ENABLED: True,
                    CONF_GUEST_GROUP_REVIEWED_ADMIN_FINGERPRINT: current_fingerprint,
                },
            )
        return self.async_show_form(
            step_id="confirm",
            data_schema=vol.Schema({}),
            description_placeholders={
                **_guest_admin_placeholders(preview),
                "group_name": self._proposal[CONF_GUEST_GROUP_NAME],
            },
        )

    async def async_step_recover(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Require an exact group ID; never discover by mutable group name."""
        manager = self._recovery_manager()
        if self._proposal is None or manager is None:
            return await self.async_step_init()
        errors: dict[str, str] = {}
        if user_input is not None:
            raw_group_id = user_input.get("recovery_group_id")
            try:
                reviewed = await manager.async_review_unknown_group(raw_group_id)
            except GuestGroupError as err:
                errors["base"] = err.reason
            else:
                self._recovery_group_id = reviewed["group_id"]
                self._recovery_bot_account = reviewed["bot_account"]
                return await self.async_step_recover_confirm()
        return self.async_show_form(
            step_id="recover",
            data_schema=vol.Schema(
                {vol.Required("recovery_group_id"): TextSelector(TextSelectorConfig())}
            ),
            errors=errors,
        )

    async def async_step_recover_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Display both immutable IDs, then reverify immediately before adoption."""
        manager = self._recovery_manager()
        if (
            self._proposal is None
            or self._recovery_group_id is None
            or self._recovery_bot_account is None
            or manager is None
        ):
            return await self.async_step_init()
        errors: dict[str, str] = {}
        if user_input is not None:
            try:
                await manager.async_adopt_unknown_group(
                    self._recovery_group_id,
                    reviewed_bot_account=self._recovery_bot_account,
                )
            except GuestGroupError as err:
                errors["base"] = err.reason
            else:
                return self.async_create_entry(
                    title="",
                    data={
                        **self.config_entry.options,
                        **self._proposal,
                        CONF_GUEST_GROUP_EVER_ENABLED: True,
                        CONF_GUEST_GROUP_REVIEWED_ADMIN_FINGERPRINT: (
                            self._reviewed_fingerprint
                        ),
                    },
                )
        return self.async_show_form(
            step_id="recover_confirm",
            data_schema=vol.Schema({}),
            errors=errors,
            description_placeholders={
                "group_id": self._recovery_group_id,
                "bot_account": self._recovery_bot_account,
            },
        )


def _guest_group_options_schema(
    preview: GuestAdminPreview, options: Mapping[str, Any]
) -> vol.Schema:
    """Offer explicit exclusions only for currently unmapped HA admins."""
    fields: dict[Any, Any] = {
        vol.Optional(
            "media_enabled", default=options.get("media_enabled", False)
        ): BooleanSelector(),
        vol.Optional(
            "outbound_observations_enabled",
            default=options.get("outbound_observations_enabled", False),
        ): BooleanSelector(),
        vol.Optional(
            "guest_group_observation_content_redacted",
            default=options.get("guest_group_observation_content_redacted", False),
        ): BooleanSelector(),
        vol.Required(
            CONF_GUEST_GROUP_ENABLED,
            default=options.get(CONF_GUEST_GROUP_ENABLED, False),
        ): BooleanSelector(),
        vol.Required(
            CONF_GUEST_GROUP_NAME,
            default=options.get(CONF_GUEST_GROUP_NAME, DEFAULT_GUEST_GROUP_NAME),
        ): TextSelector(TextSelectorConfig()),
    }
    if preview.gaps:
        prior_exclusions = options.get(CONF_GUEST_GROUP_EXCLUDED_ADMIN_USER_IDS, [])
        gap_ids = {gap.user_id for gap in preview.gaps}
        defaults = [user_id for user_id in prior_exclusions if user_id in gap_ids]
        fields[
            vol.Optional(CONF_GUEST_GROUP_EXCLUDED_ADMIN_USER_IDS, default=defaults)
        ] = SelectSelector(
            SelectSelectorConfig(
                options=[
                    {"value": gap.user_id, "label": f"{gap.name} ({gap.reason})"}
                    for gap in preview.gaps
                ],
                multiple=True,
            )
        )
    return vol.Schema(fields)


def _guest_admin_placeholders(preview: GuestAdminPreview) -> dict[str, str]:
    """Summarize only this entry's mapped and unmapped administrators."""
    mapped = [
        f"{contact.name} ({contact.person_entity_id}, {contact.recipient})"
        for contact in preview.contacts
    ]
    gaps = [f"{gap.name} ({gap.reason})" for gap in preview.gaps]
    return {
        "mapped_admins": ", ".join(mapped) or "None",
        "unmapped_admins": ", ".join(gaps) or "None",
    }


class RecipientSubentryFlow(ConfigSubentryFlow):
    """Create or reconfigure an individual notification recipient."""

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        """Add a WhatsApp recipient."""
        return await self._async_step_recipient(user_input, step_id="user")

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        """Modify an existing WhatsApp recipient."""
        return await self._async_step_recipient(
            user_input,
            step_id="reconfigure",
            reconfigure_subentry=self._get_reconfigure_subentry(),
        )

    async def _async_step_recipient(
        self,
        user_input: dict[str, Any] | None,
        *,
        step_id: str,
        reconfigure_subentry: ConfigSubentry | None = None,
    ) -> SubentryFlowResult:
        """Validate and store an individual contact."""
        entry = self._get_entry()
        if entry.state is not ConfigEntryState.LOADED:
            return self.async_abort(
                reason="entry_not_loaded",
                description_placeholders={"entry_title": entry.title},
            )

        errors: dict[str, str] = {}
        placeholders: dict[str, str] = {}
        if user_input is not None:
            person_entity_id = (
                str(user_input.get(CONF_PERSON_ENTITY_ID, "")).strip() or None
            )
            person_state = (
                self.hass.states.get(person_entity_id)
                if person_entity_id is not None
                else None
            )
            if person_entity_id is not None and (
                person_state is None or person_entity_id.split(".", 1)[0] != "person"
            ):
                errors["base"] = "person_not_found"
            else:
                name = str(user_input.get(CONF_NAME, "")).strip()
                if not name and person_state is not None:
                    name = person_state.name
                if not name:
                    errors["base"] = "invalid_name"
                else:
                    try:
                        recipient = normalize_recipient(user_input[CONF_RECIPIENT])
                    except ValueError as err:
                        errors["base"] = "invalid_recipient"
                        placeholders["error"] = str(err)
                    else:
                        existing = [
                            subentry
                            for subentry in entry.subentries.values()
                            if reconfigure_subentry is None
                            or subentry.subentry_id != reconfigure_subentry.subentry_id
                        ]
                        if person_entity_id is not None and any(
                            subentry.data.get(CONF_PERSON_ENTITY_ID) == person_entity_id
                            for subentry in existing
                        ):
                            return self.async_abort(reason="person_already_configured")
                        if any(
                            subentry.data.get(CONF_RECIPIENT) == recipient
                            for subentry in existing
                        ):
                            return self.async_abort(reason="phone_already_configured")

                        data = {CONF_RECIPIENT: recipient}
                        # Preserve legacy contact data shape when observation is off.
                        if user_input.get("observation_content_redacted") is True:
                            data["observation_content_redacted"] = True
                        if person_entity_id is not None:
                            data[CONF_PERSON_ENTITY_ID] = person_entity_id
                        if reconfigure_subentry is not None:
                            return self.async_update_and_abort(
                                entry,
                                reconfigure_subentry,
                                title=name,
                                data=data,
                            )
                        return self.async_create_entry(
                            title=name,
                            data=data,
                        )

        suggested_values = dict(user_input or {})
        if user_input is None and reconfigure_subentry is not None:
            suggested_values = {
                **reconfigure_subentry.data,
                CONF_NAME: reconfigure_subentry.title,
            }

        return self.async_show_form(
            step_id=step_id,
            data_schema=self.add_suggested_values_to_schema(
                RECIPIENT_SCHEMA, suggested_values
            ),
            errors=errors,
            description_placeholders=placeholders,
        )


class GeneralGroupSubentryFlow(ConfigSubentryFlow):
    """Explicitly configure an existing group without changing WhatsApp membership."""

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        return await self._async_step_group(user_input, step_id="user")

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        return await self._async_step_group(
            user_input,
            step_id="reconfigure",
            existing=self._get_reconfigure_subentry(),
        )

    async def _async_step_group(
        self,
        user_input: dict[str, Any] | None,
        *,
        step_id: str,
        existing: ConfigSubentry | None = None,
    ) -> SubentryFlowResult:
        entry = self._get_entry()
        if entry.state is not ConfigEntryState.LOADED:
            return self.async_abort(
                reason="entry_not_loaded",
                description_placeholders={"entry_title": entry.title},
            )
        errors: dict[str, str] = {}
        if user_input is not None:
            name = str(user_input.get(CONF_NAME, "")).strip()
            if not name or len(name) > 100:
                errors["base"] = "invalid_name"
            else:
                try:
                    group_id = normalize_group_id(user_input.get(CONF_GROUP_ID))
                    if any(
                        subentry.subentry_type == SUBENTRY_TYPE_GROUP
                        and group_id_matches(subentry.data.get(CONF_GROUP_ID), group_id)
                        and (
                            existing is None
                            or subentry.subentry_id != existing.subentry_id
                        )
                        for subentry in entry.subentries.values()
                    ):
                        return self.async_abort(reason="group_already_configured")
                    enabled = user_input.get(CONF_GROUP_ENABLED) is True
                    # A disabled existing destination must be removable from use
                    # even while WAHA is offline. New/enabled bindings always prove
                    # the exact account, exact group and current bot membership.
                    if (
                        not enabled
                        and existing is not None
                        and group_id_matches(existing.data.get(CONF_GROUP_ID), group_id)
                    ):
                        account = existing.data[CONF_GROUP_ACCOUNT]
                    else:
                        runtime = getattr(entry, "runtime_data", None)
                        manager = getattr(runtime, "general_groups", None)
                        if manager is None:
                            raise GeneralGroupError("group_unverified")
                        proof = await manager.async_review(group_id)
                        account = proof.bot_account
                except GeneralGroupError as err:
                    errors["base"] = err.reason
                except WahaError:
                    errors["base"] = "group_unverified"
                else:
                    data = {
                        CONF_GROUP_ID: group_id,
                        CONF_GROUP_ENABLED: enabled,
                        CONF_GROUP_ACCOUNT: account,
                        "observation_content_redacted": (
                            user_input.get("observation_content_redacted") is True
                        ),
                    }
                    if existing is not None:
                        return self.async_update_and_abort(
                            entry, existing, title=name, data=data
                        )
                    return self.async_create_entry(title=name, data=data)
        suggested = dict(user_input or {})
        if user_input is None and existing is not None:
            suggested = {**existing.data, CONF_NAME: existing.title}
        return self.async_show_form(
            step_id=step_id,
            data_schema=self.add_suggested_values_to_schema(
                vol.Schema(
                    {
                        vol.Required(CONF_NAME): TextSelector(TextSelectorConfig()),
                        vol.Required(CONF_GROUP_ID): TextSelector(TextSelectorConfig()),
                        vol.Required(
                            CONF_GROUP_ENABLED, default=False
                        ): BooleanSelector(),
                        vol.Optional(
                            "observation_content_redacted", default=False
                        ): BooleanSelector(),
                    }
                ),
                suggested,
            ),
            errors=errors,
        )


async def _validate_account(
    hass: HomeAssistant, data: Mapping[str, Any]
) -> tuple[WahaServer, WahaSession]:
    """Validate a WAHA API connection and configured session."""
    client = WahaClient(
        async_get_clientsession(hass),
        data[CONF_API_KEY],
        data[CONF_SESSION],
        base_url=data[CONF_API_URL],
    )
    server = await client.async_get_server()
    session = await client.async_get_session()
    return server, session


def _normalize_account_data(data: Mapping[str, Any]) -> dict[str, Any]:
    """Normalize user-entered connection data."""
    return {
        CONF_API_URL: str(data[CONF_API_URL]).strip().rstrip("/"),
        CONF_API_KEY: str(data[CONF_API_KEY]),
        CONF_SESSION: str(data[CONF_SESSION]).strip(),
    }


def _with_new_webhook_credentials(data: Mapping[str, Any]) -> dict[str, Any]:
    """Create private callback credentials for a new config entry."""
    return {
        **data,
        CONF_WEBHOOK_ID: secrets.token_hex(32),
        CONF_WEBHOOK_SECRET: secrets.token_urlsafe(48),
    }


def _account_data_from_discovery(info: HassioServiceInfo) -> dict[str, Any]:
    """Convert Supervisor discovery data into a config entry payload."""
    host = str(info.config["host"]).strip()
    port = int(info.config.get("port", 3000))
    if not host or not 1 <= port <= 65535:
        raise ValueError("Invalid discovery host or port")
    return {
        CONF_API_URL: f"http://{host}:{port}",
        CONF_API_KEY: str(info.config["api_key"]),
        CONF_SESSION: str(info.config.get("session", DEFAULT_SESSION)).strip(),
        CONF_ADDON_SLUG: info.slug,
    }


def _manual_unique_id(data: Mapping[str, Any]) -> str:
    """Create a stable unique ID for a manually configured session."""
    return f"manual:{data[CONF_API_URL]}:{data[CONF_SESSION]}"


def _entry_title(server: WahaServer, session: WahaSession) -> str:
    """Build a useful entry title without exposing credentials."""
    identity = session.push_name or session.name
    return f"WAHA {identity} ({server.version})"
