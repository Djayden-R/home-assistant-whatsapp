"""Normalize authenticated WAHA callbacks into a private Home Assistant channel.

The webhook caller authenticates the raw request before invoking this manager.
Only configured direct conversations are published; no raw WAHA identity is
ever copied to the Home Assistant event bus.
"""

from __future__ import annotations

import logging
import math
import re
import time
from collections import Counter
from collections.abc import Awaitable, Callable, Mapping
from copy import deepcopy
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from homeassistant.core import Context

from .activity import incoming_activity_provenance
from .const import CHANNEL_SCHEMA_VERSION, CONF_SESSION, EVENT_WAHA_WHATSAPP
from .media import media_metadata

if TYPE_CHECKING:
    from homeassistant.config_entries import ConfigEntry
    from homeassistant.core import HomeAssistant

    from .channel_registry import ChannelRegistry
    from .commands import CommandRegistry
    from .guest_registry import GuestRegistry

_LOGGER = logging.getLogger(__name__)

SCHEMA_VERSION = CHANNEL_SCHEMA_VERSION
MAX_TEXT_LENGTH = 8192
MAX_EMOJI_LENGTH = 32
MAX_MIME_LENGTH = 128
MAX_ID_LENGTH = 512
MAX_EVENT_AGE_SECONDS = 60 * 60
MAX_FUTURE_SKEW_SECONDS = 5 * 60
_DIRECT_CHAT = re.compile(
    r"^(?:[0-9]+@(?:c\.us|s\.whatsapp\.net)|[0-9]+(?::[0-9]+)?@lid)$",
    re.IGNORECASE,
)
_MIME_TYPE = re.compile(
    r"^[a-z0-9][a-z0-9!#$&^_.+-]*/[a-z0-9][a-z0-9!#$&^_.+-]*$",
    re.IGNORECASE,
)


class WahaInboundManager:
    """Publish safe, versioned inbound events for one WAHA config entry."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        registry: ChannelRegistry,
        guest_registry: GuestRegistry | None = None,
        refresh_guest_membership: Callable[[], Awaitable[dict[str, Any]]] | None = None,
        command_registry: CommandRegistry | None = None,
        media_manager: Any | None = None,
    ) -> None:
        """Keep routing and deduplication delegated to the channel registry."""
        self._hass = hass
        self._entry = entry
        self._registry = registry
        self._guest_registry = guest_registry
        self._refresh_guest_membership = refresh_guest_membership
        self._command_registry = command_registry
        self._media_manager = media_manager
        self._rejections: Counter[str] = Counter()
        self.accepted_count = 0

    @property
    def rejected_event_count(self) -> int:
        """Return the number of authenticated callbacks dropped after parsing."""
        return sum(self._rejections.values())

    @property
    def rejection_reasons(self) -> dict[str, int]:
        """Expose redacted diagnostics without content or contact identifiers."""
        return dict(sorted(self._rejections.items()))

    async def async_handle_payload(self, data: Mapping[str, Any]) -> None:
        """Handle one HMAC-verified WAHA event without interpreting its text."""
        event = data.get("event")
        if event not in ("message", "message.reaction"):
            self._reject("unsupported_event")
            return
        if data.get("session") != self._entry.data[CONF_SESSION]:
            self._reject("wrong_session")
            return

        payload = data.get("payload")
        if not isinstance(payload, Mapping):
            self._reject("invalid_payload")
            return
        if payload.get("fromMe") is not False:
            self._reject("wrong_direction")
            return

        raw_chat = payload.get("from")
        if not _bounded_string(raw_chat, MAX_ID_LENGTH) or not _DIRECT_CHAT.fullmatch(
            raw_chat
        ):
            self._reject("not_direct_chat")
            return
        # A direct-chat reaction may include participant=sender. A group or
        # broadcast destination must never enter the direct-contact channel.
        destination = payload.get("to")
        if destination is not None and (
            not isinstance(destination, str)
            or destination.endswith(("@g.us", "@broadcast", "@newsletter"))
        ):
            self._reject("not_direct_chat")
            return

        # WAHA can put a participant's direct identity in `from` even when
        # the containing chat is a group. Validate the chat independently.
        raw_conversation = payload.get("chatId")
        if raw_conversation is not None and (
            not _bounded_string(raw_conversation, MAX_ID_LENGTH)
            or not _DIRECT_CHAT.fullmatch(raw_conversation)
        ):
            self._reject("not_direct_chat")
            return
        raw_participant = payload.get("participant")
        if raw_participant is not None and (
            not _bounded_string(raw_participant, MAX_ID_LENGTH)
            or not _DIRECT_CHAT.fullmatch(raw_participant)
        ):
            self._reject("not_direct_chat")
            return

        contact = await self._registry.async_resolve_chat(raw_chat)
        if contact is None:
            self._reject("unknown_contact")
            return
        for correlated_id in (raw_conversation, raw_participant):
            if correlated_id is None:
                continue
            correlated_contact = await self._registry.async_resolve_chat(correlated_id)
            if (
                correlated_contact is None
                or correlated_contact.conversation_id != contact.conversation_id
            ):
                self._reject("chat_identity_mismatch")
                return

        event_timestamp = _event_timestamp(
            payload.get("timestamp"), data.get("timestamp")
        )
        if event_timestamp is None:
            self._reject("invalid_timestamp")
            return
        now = time.time()
        if event_timestamp < now - MAX_EVENT_AGE_SECONDS:
            self._reject("stale_event")
            return
        if event_timestamp > now + MAX_FUTURE_SKEW_SECONDS:
            self._reject("future_event")
            return
        occurred_at = datetime.fromtimestamp(event_timestamp, tz=UTC).isoformat()

        raw_message_id = payload.get("id")
        if not _bounded_string(raw_message_id, MAX_ID_LENGTH):
            self._reject("invalid_message_id")
            return

        if event == "message":
            detail = self._message_detail(payload)
        else:
            detail = await self._reaction_detail(payload, contact.conversation_id)
        if detail is None:
            return

        raw_event_id = _event_identity(data, payload)
        if raw_event_id is None:
            self._reject("invalid_event_id")
            return

        media_route_binding = (
            self._media_manager.capture_observation_binding(
                contact.conversation_id, raw_participant=payload.get("participant")
            )
            if self._media_manager is not None
            else None
        )
        if event == "message":
            message = detail["message"]
            message["id"] = await self._registry.async_remember_message(
                raw_message_id,
                contact.conversation_id,
                **incoming_activity_provenance(
                    self._entry,
                    contact.conversation_id,
                    raw_message_id,
                    payload.get("chatId") or raw_chat,
                ),
            )
        if not await self._registry.async_claim_event(raw_event_id):
            self._reject("duplicate")
            return

        if event == "message" and payload.get("hasMedia") and self._media_manager:
            detail["message"]["attachments"] = await self._media_manager.async_observe(
                payload,
                detail["message"]["id"],
                contact.conversation_id,
                raw_chat_id=payload.get("chatId") or raw_chat,
                route_binding=media_route_binding,
            )

        event_data: dict[str, Any] = {
            "schema_version": SCHEMA_VERSION,
            "event_id": self._registry.event_token(raw_event_id),
            "type": (
                "message.received"
                if event == "message"
                else (
                    "reaction.added"
                    if detail["reaction"]["emoji"]
                    else "reaction.removed"
                )
            ),
            "config_entry_id": self._entry.entry_id,
            "conversation_id": contact.conversation_id,
            "conversation_type": "direct",
            "occurred_at": occurred_at,
            "sender": contact.event_sender(),
            **detail,
        }
        # A configured contact keeps its independent direct conversation.
        # Current guest membership is additive metadata only, never a second
        # event or a replacement for its existing notify/contact identity.
        if self._guest_registry is not None:
            member = self._guest_registry.resolve_member(
                raw_chat, occurred_at=event_timestamp
            )
            if member is not None and self._refresh_guest_membership is not None:
                try:
                    refreshed = await self._refresh_guest_membership()
                except Exception:
                    refreshed = {"ready": False}
                member = (
                    self._guest_registry.resolve_member(
                        raw_chat, occurred_at=event_timestamp
                    )
                    if refreshed.get("ready")
                    else None
                )
            if member is not None:
                group = self._guest_registry.snapshot()
                if group["ready"]:
                    event_data["sender"]["participant_id"] = member["participant_id"]
                    event_data["group"] = {
                        "group_id": group["group_id"],
                        "purpose": "guests",
                    }
                    event_data["membership"] = {
                        "membership_id": member["membership_id"],
                        "kind": member["kind"],
                        "status": member["status"],
                        "whatsapp_role": member["whatsapp_role"],
                    }
                    if direct_route := member.get("direct_conversation_id"):
                        event_data["membership"]["direct_conversation_id"] = (
                            direct_route
                        )
        context = await self._async_person_context(contact.person_entity_id)
        # HA callback listeners can run synchronously and mutate event data.
        # Never use the public bus copy to authorize a command request.
        self._hass.bus.async_fire(
            EVENT_WAHA_WHATSAPP, deepcopy(event_data), context=deepcopy(context)
        )
        if self._command_registry is not None:
            try:
                self._command_registry.publish_from_message(event_data, context=context)
            except Exception:
                _LOGGER.exception("WAHA command recognition failed")
        self.accepted_count += 1

    def _message_detail(
        self, payload: Mapping[str, Any]
    ) -> dict[str, dict[str, Any]] | None:
        """Build a text message or a metadata-only media placeholder."""
        body = payload.get("body")
        has_media = payload.get("hasMedia")
        if not isinstance(has_media, bool):
            self._reject("invalid_content")
            return None
        if body is None and has_media:
            body = ""
        if not isinstance(body, str) or len(body) > MAX_TEXT_LENGTH:
            self._reject("invalid_content")
            return None

        reply_to = payload.get("replyTo")
        in_reply_to: str | None = None
        if reply_to is not None:
            if not isinstance(reply_to, Mapping):
                self._reject("invalid_reply")
                return None
            raw_reply_id = reply_to.get("id")
            if raw_reply_id is not None:
                if not _bounded_string(raw_reply_id, MAX_ID_LENGTH):
                    self._reject("invalid_reply")
                    return None
                in_reply_to = self._registry.message_token(raw_reply_id)

        message: dict[str, Any] = {"in_reply_to": in_reply_to}
        if has_media:
            media = payload.get("media")
            if media is not None and not isinstance(media, Mapping):
                self._reject("invalid_media")
                return None
            mime = media.get("mimetype") if isinstance(media, Mapping) else None
            if mime is not None:
                if not _bounded_string(mime, MAX_MIME_LENGTH):
                    self._reject("invalid_media")
                    return None
                mime = mime.split(";", 1)[0].strip().lower()
                if not _MIME_TYPE.fullmatch(mime):
                    self._reject("invalid_media")
                    return None
            kind = media_metadata(payload)["kind"]
            message["kind"] = kind
            if mime is not None:
                message["mime_type"] = mime
            if body:
                message["caption"] = body
        else:
            if not body:
                self._reject("invalid_content")
                return None
            message.update({"kind": "text", "text": body})
        return {"message": message}

    async def _reaction_detail(
        self, payload: Mapping[str, Any], conversation_id: str
    ) -> dict[str, dict[str, Any]] | None:
        """Report an emoji change without assigning any action semantics."""
        reaction = payload.get("reaction")
        if not isinstance(reaction, Mapping):
            self._reject("invalid_reaction")
            return None
        emoji = reaction.get("text")
        target = reaction.get("messageId")
        if (
            not isinstance(emoji, str)
            or len(emoji) > MAX_EMOJI_LENGTH
            or not _bounded_string(target, MAX_ID_LENGTH)
        ):
            self._reject("invalid_reaction")
            return None
        known = await self._registry.async_message_known(target, conversation_id)
        return {
            "reaction": {
                "emoji": emoji,
                "target_message_id": self._registry.message_token(target),
                "target_known": known,
            }
        }

    async def _async_person_context(
        self, person_entity_id: str | None
    ) -> Context | None:
        """Attribute an event to an active linked user, not authorize actions."""
        if not person_entity_id:
            return None
        person_state = self._hass.states.get(person_entity_id)
        if person_state is None:
            return None
        user_id = person_state.attributes.get("user_id")
        if not isinstance(user_id, str) or not user_id:
            return None
        user = await self._hass.auth.async_get_user(user_id)
        if user is None or not user.is_active:
            return None
        return Context(user_id=user.id)

    def _reject(self, reason: str) -> None:
        """Count a safe reason, deliberately never logging message content."""
        self._rejections[reason] += 1
        _LOGGER.debug("Discarded WAHA inbound event (%s)", reason)


def _bounded_string(value: object, limit: int) -> bool:
    """Return whether an opaque WAHA field is a nonempty bounded string."""
    return isinstance(value, str) and 0 < len(value) <= limit


def _event_timestamp(payload_time: object, envelope_time: object) -> float | None:
    """Read WAHA message seconds or webhook milliseconds, preferring message time."""
    value = payload_time if payload_time is not None else envelope_time
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    if not math.isfinite(value) or value <= 0:
        return None
    seconds = value / 1000 if value >= 1e11 else value
    try:
        datetime.fromtimestamp(seconds, tz=UTC)
    except OverflowError, OSError, ValueError:
        return None
    return seconds


def _event_identity(data: Mapping[str, Any], payload: Mapping[str, Any]) -> str | None:
    """Use the stable WAHA event ID, falling back to old-format event fields."""
    event_id = data.get("id")
    if _bounded_string(event_id, MAX_ID_LENGTH):
        return event_id
    message_id = payload.get("id")
    timestamp = payload.get("timestamp")
    if not _bounded_string(message_id, MAX_ID_LENGTH) or isinstance(timestamp, bool):
        return None
    if not isinstance(timestamp, int | float) or not math.isfinite(timestamp):
        return None
    event = data.get("event")
    if event == "message.reaction":
        reaction = payload.get("reaction")
        emoji = reaction.get("text") if isinstance(reaction, Mapping) else None
        target = reaction.get("messageId") if isinstance(reaction, Mapping) else None
        if not isinstance(emoji, str) or not _bounded_string(target, MAX_ID_LENGTH):
            return None
        return f"fallback:{event}:{message_id}:{timestamp}:{target}:{emoji}"
    return f"fallback:{event}:{message_id}:{timestamp}"


def _media_kind(mime: str | None) -> str:
    """Classify without fetching or exposing any WAHA media URL."""
    if mime is None:
        return "unknown"
    mime = mime.lower()
    if mime.startswith("audio/"):
        return "audio"
    if mime.startswith("image/"):
        return "image"
    return "file"
