"""Tests for HACS and Home Assistant metadata files."""

import json
import tomllib
from pathlib import Path

import yaml

ROOT = Path(__file__).parents[1]
INTEGRATION = ROOT / "custom_components" / "waha_whatsapp"


def test_json_metadata_is_valid() -> None:
    """All JSON metadata parses and English translations match strings."""
    manifest = json.loads((INTEGRATION / "manifest.json").read_text())
    hacs = json.loads((ROOT / "hacs.json").read_text())
    strings = json.loads((INTEGRATION / "strings.json").read_text())
    english = json.loads((INTEGRATION / "translations" / "en.json").read_text())

    assert manifest["domain"] == "waha_whatsapp"
    assert manifest["config_flow"] is True
    assert (
        manifest["version"]
        == tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]["version"]
    )
    assert manifest["codeowners"] == ["@sebastian-greco"]
    assert manifest["issue_tracker"].endswith("/issues")
    assert hacs["name"] == "WAHA WhatsApp"
    assert english == strings


def test_service_metadata_matches_actions() -> None:
    """All public WAHA actions have editor metadata."""
    services = yaml.safe_load((INTEGRATION / "services.yaml").read_text())
    assert set(services) == {
        "get_channel_capabilities",
        "get_group_membership",
        "get_media",
        "list_commands",
        "register_command",
        "send_message",
        "send_media_to_conversation",
        "send_poll",
        "send_poll_to_conversation",
        "get_poll_selections",
        "send_to_conversation",
        "unregister_command",
    }


def test_recipient_flow_uses_home_assistant_people() -> None:
    """Recipient setup can link optional Person metadata to a contact."""
    source = (INTEGRATION / "config_flow.py").read_text()
    assert 'domain="person"' in source
    assert "vol.Optional(CONF_PERSON_ENTITY_ID)" in source
    assert "vol.Optional(CONF_NAME)" in source
    assert "CONF_CONTACT_ROLE" not in source
    assert "CONF_GROUP_ADULTS" not in source


def test_recipient_changes_use_one_reload_listener() -> None:
    """All entry and subentry changes reload individual recipient entities."""
    integration = (INTEGRATION / "__init__.py").read_text()
    config_flow = (INTEGRATION / "config_flow.py").read_text()

    assert "entry.add_update_listener(_async_reload_entry)" in integration
    assert "await hass.config_entries.async_reload(entry.entry_id)" in integration
    assert "async_update_reload_and_abort" not in config_flow
    assert "reload_on_update=False" in config_flow


def test_notify_entities_share_stable_waha_device_name() -> None:
    """Account identity and server version do not leak into entity IDs."""
    source = (INTEGRATION / "notify.py").read_text()

    assert 'name="WAHA"' in source
    assert "name=config_entry.title" not in source


def test_notify_devices_are_scoped_to_recipient_subentries() -> None:
    """Home Assistant 2026.8 must not move one contact's device to another."""
    source = (INTEGRATION / "notify.py").read_text()

    assert "_device_info(config_entry, subentry)" in source
    assert "subentry.unique_id or subentry.subentry_id" in source
    assert (
        'f"{config_entry.unique_id or config_entry.entry_id}:{subentry_id}"' in source
    )


def test_notify_platform_preserves_individual_contacts_and_opt_in_guest_group() -> None:
    """The managed WhatsApp group is separate from removed household fan-outs."""
    source = (INTEGRATION / "notify.py").read_text()

    assert "WahaGuestGroupNotifyEntity" in source
    assert "RECIPIENT_GROUP_" not in source
    assert "config_entry.subentries.items()" in source


def test_notify_entities_expose_safe_person_metadata() -> None:
    """Individual entities publish only the template-safe Person association."""
    source = (INTEGRATION / "notify.py").read_text()

    assert "_attr_extra_state_attributes = contact_state_attributes(" in source


def test_config_entry_migration_is_registered() -> None:
    """Version 1.3 safely removes groups and adds private webhook credentials."""
    integration = (INTEGRATION / "__init__.py").read_text()
    config_flow = (INTEGRATION / "config_flow.py").read_text()

    assert "MINOR_VERSION = 3" in config_flow
    assert "async def async_migrate_entry(" in integration
    assert "async_update_subentry(" in integration
    assert "async_entries_for_config_entry(" in integration
    assert "entity.platform == DOMAIN" in integration
    assert "entity.unique_id in obsolete_unique_ids" in integration
    assert "with_webhook_credentials(" in integration


def test_actionable_polls_reuse_companion_app_event_contract() -> None:
    """Existing action automations can consume settled WhatsApp poll votes."""
    constants = (INTEGRATION / "const.py").read_text()
    manager = (INTEGRATION / "poll_manager.py").read_text()

    assert (
        'EVENT_MOBILE_APP_NOTIFICATION_ACTION: Final = "mobile_app_notification_action"'
        in constants
    )
    assert "EVENT_MOBILE_APP_NOTIFICATION_ACTION" in manager
    assert '{"action": action}' in manager
    assert "async_fire(" in manager
    assert "async_call(" not in manager


def test_poll_events_attribute_linked_active_home_assistant_user() -> None:
    """Voter identity uses Person metadata and event context, not event data."""
    integration = (INTEGRATION / "__init__.py").read_text()
    manager = (INTEGRATION / "poll_manager.py").read_text()
    polls = (INTEGRATION / "polls.py").read_text()

    assert "CONF_PERSON_ENTITY_ID" in integration
    assert "person_entity_id: str | None" in polls
    assert '"person_entity_id": self.person_entity_id' in polls
    assert '"user_id"' not in polls
    assert 'PERSON_USER_ID_ATTRIBUTE = "user_id"' in manager
    assert "homeassistant.components.person.const" not in manager
    assert "self._hass.auth.async_get_user(user_id)" in manager
    assert "not user.is_active" in manager
    assert "Context(user_id=user.id)" in manager
    assert "EventOrigin" not in manager
    assert "context=context" in manager


def test_poll_webhook_is_private_and_authenticated() -> None:
    """Inbound poll actions require a local callback and WAHA HMAC."""
    integration = (INTEGRATION / "__init__.py").read_text()
    manager = (INTEGRATION / "poll_manager.py").read_text()

    assert "local_only=True" in integration
    assert 'algorithm != "sha512"' in integration
    assert "verify_signature(raw_body, signature)" in integration
    assert "poll.vote.failed" in manager


def test_poll_secrets_are_redacted_from_diagnostics() -> None:
    """Private callback credentials are not exposed in downloaded diagnostics."""
    diagnostics = (INTEGRATION / "diagnostics.py").read_text()

    assert "CONF_WEBHOOK_ID" in diagnostics
    assert "CONF_WEBHOOK_SECRET" in diagnostics
    assert "TO_REDACT" in diagnostics
    assert '"rejected_votes"' in diagnostics
    assert '"rejection_reasons"' in diagnostics


def test_bidirectional_channel_reuses_private_webhook() -> None:
    """New message handling must not bypass poll webhook authentication."""
    integration = (INTEGRATION / "__init__.py").read_text()
    api = (INTEGRATION / "api.py").read_text()
    notify = (INTEGRATION / "notify.py").read_text()
    diagnostics = (INTEGRATION / "diagnostics.py").read_text()

    assert "SERVICE_SEND_TO_CONVERSATION" in integration
    assert "_async_handle_send_to_conversation" in integration
    assert '"message.reaction"' in api
    assert '"message"' in api
    assert "verify_signature(raw_body, signature)" in integration
    assert "MAX_WEBHOOK_BODY_BYTES" in integration
    assert "await inbound_manager.async_handle_payload(payload)" in integration
    outbound = (INTEGRATION / "outbound.py").read_text()
    assert "async_send_observed(" in notify
    assert "await self._registry.async_remember_message(" in outbound
    assert "contact.conversation_id" in notify
    assert '"inbound_channel"' in diagnostics


def test_actionable_poll_responsibilities_are_documented() -> None:
    """The guide keeps execution, expiry, and confirmation in automations."""
    readme = (ROOT / "README.md").read_text()
    migration_guide = (ROOT / "docs" / "NOTIFICATION_MIGRATION.md").read_text()

    assert "mobile_app_notification_action" in readme
    assert "does not call services, enforce workflow expiration" in readme
    assert "It does not perform the" in migration_guide
    assert "action, create an expiry timer, or send a confirmation" in migration_guide
