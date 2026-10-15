"""Constants for the WAHA WhatsApp integration."""

from typing import Final

DOMAIN: Final = "waha_whatsapp"

DEFAULT_API_URL: Final = "http://localhost:3000"
DEFAULT_SESSION: Final = "default"
DEFAULT_GUEST_GROUP_NAME: Final = "Guests"

CONF_GUEST_GROUP_ENABLED: Final = "guest_group_enabled"
CONF_GUEST_GROUP_EVER_ENABLED: Final = "guest_group_ever_enabled"
CONF_GUEST_GROUP_NAME: Final = "guest_group_name"
CONF_GUEST_GROUP_EXCLUDED_ADMIN_USER_IDS: Final = "guest_group_excluded_admin_user_ids"
CONF_GUEST_GROUP_REVIEWED_ADMIN_FINGERPRINT: Final = (
    "guest_group_reviewed_admin_fingerprint"
)

CONF_API_URL: Final = "api_url"
CONF_SESSION: Final = "session"
CONF_PERSON_ENTITY_ID: Final = "person_entity_id"
CONF_RECIPIENT: Final = "recipient"
CONF_ADDON_SLUG: Final = "addon_slug"
CONF_WEBHOOK_ID: Final = "webhook_id"
CONF_WEBHOOK_SECRET: Final = "webhook_secret"
CONF_OUTBOUND_OBSERVATIONS_ENABLED: Final = "outbound_observations_enabled"
CONF_OBSERVATION_CONTENT_REDACTED: Final = "observation_content_redacted"

SUBENTRY_TYPE_RECIPIENT: Final = "recipient"
SUBENTRY_TYPE_GROUP: Final = "group"

ATTR_TO: Final = "to"
ATTR_TITLE: Final = "title"
ATTR_MESSAGE: Final = "message"
ATTR_LINK_PREVIEW: Final = "link_preview"
ATTR_PERSON_ENTITY_ID: Final = "person_entity_id"
ATTR_ACTIONS: Final = "actions"
ATTR_ENTITY_ID: Final = "entity_id"
ATTR_NO_ACTION_TITLE: Final = "no_action_title"
ATTR_SETTLE_SECONDS: Final = "settle_seconds"
ATTR_CONVERSATION_ID: Final = "conversation_id"
ATTR_REPLY_TO_MESSAGE_ID: Final = "reply_to_message_id"
ATTR_CLIENT_REQUEST_ID: Final = "client_request_id"
ATTR_ORIGIN: Final = "origin"
ATTR_CONTENT_REDACTED: Final = "content_redacted"

SERVICE_SEND_MESSAGE: Final = "send_message"
SERVICE_SEND_POLL: Final = "send_poll"
SERVICE_SEND_TO_CONVERSATION: Final = "send_to_conversation"
SERVICE_GET_CHANNEL_CAPABILITIES: Final = "get_channel_capabilities"
SERVICE_GET_GROUP_MEMBERSHIP: Final = "get_group_membership"
SERVICE_REGISTER_COMMAND: Final = "register_command"
SERVICE_UNREGISTER_COMMAND: Final = "unregister_command"
SERVICE_LIST_COMMANDS: Final = "list_commands"
EVENT_WAHA_WHATSAPP: Final = "waha_whatsapp_event"
CHANNEL_SCHEMA_VERSION: Final = 1

DEFAULT_NO_ACTION_TITLE: Final = "No action"
DEFAULT_SETTLE_SECONDS: Final = 5.0
EVENT_MOBILE_APP_NOTIFICATION_ACTION: Final = "mobile_app_notification_action"

SESSION_STATUS_WORKING: Final = "WORKING"
