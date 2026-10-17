"""Publish confirmed managed-group traffic without exposing WhatsApp identities.

The caller authenticates WAHA's webhook and verifies the current bot account
before passing an envelope here. This manager never creates groups or sends.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Awaitable, Callable, Mapping
from copy import deepcopy
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from .const import CHANNEL_SCHEMA_VERSION, CONF_SESSION, EVENT_WAHA_WHATSAPP
from .helpers import recipient_chat_id
from .inbound import (
    MAX_EVENT_AGE_SECONDS,
    MAX_FUTURE_SKEW_SECONDS,
    MAX_ID_LENGTH,
    WahaInboundManager,
    _bounded_string,
    _event_identity,
    _event_timestamp,
)

if TYPE_CHECKING:
    from homeassistant.config_entries import ConfigEntry
    from homeassistant.core import HomeAssistant

    from .channel_registry import ChannelRegistry
    from .commands import CommandRegistry
    from .guest_registry import GuestRegistry

_LOGGER = logging.getLogger(__name__)


class WahaGuestInboundManager:
    """Normalize only the exact managed group and confirmed member DM routes."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        guest_registry: GuestRegistry,
        channel_registry: ChannelRegistry,
        *,
        verified_bot_jid: str,
        refresh_membership: Callable[[], Awaitable[dict[str, Any]]] | None = None,
        command_registry: CommandRegistry | None = None,
        media_manager: Any | None = None,
    ) -> None:
        self._hass = hass
        self._entry = entry
        self._guests = guest_registry
        self._channel = channel_registry
        self._verified_bot_jid = verified_bot_jid
        self._refresh_membership = refresh_membership
        self._command_registry = command_registry
        self._media_manager = media_manager
        # Reuse the existing, intentionally narrow content/reaction parser.
        self._detail_parser = WahaInboundManager(hass, entry, channel_registry)

    def update_verified_bot_jid(self, verified_bot_jid: str) -> None:
        """Refresh the positive account proof after a WAHA reconnect."""
        self._verified_bot_jid = verified_bot_jid

    async def async_handle_payload(self, data: Mapping[str, Any]) -> bool:
        """Publish one safe event; return False when another handler should try."""
        event = data.get("event")
        if event not in ("message", "message.reaction"):
            return False
        if data.get("session") != self._entry.data[CONF_SESSION]:
            return False
        payload = data.get("payload")
        if not isinstance(payload, Mapping) or payload.get("fromMe") is not False:
            return False
        raw_from = payload.get("from")
        if not _bounded_string(raw_from, MAX_ID_LENGTH):
            return False

        snapshot = self._guests.snapshot()
        group_jid = self._guests.saved_group_jid
        if (
            not snapshot["ready"]
            or group_jid is None
            or not self._guests.group_matches(group_jid, self._verified_bot_jid)
        ):
            return False

        chat_id = payload.get("chatId")
        is_group = raw_from == group_jid or chat_id == group_jid
        if is_group:
            if chat_id not in (None, group_jid) or (
                raw_from.endswith("@g.us") and raw_from != group_jid
            ):
                return False
            sender_alias = payload.get("participant")
            if sender_alias is None and raw_from != group_jid:
                sender_alias = raw_from
            conversation_id = snapshot["conversation_id"]
            conversation_type = "group"
        else:
            # Another group, broadcast, or non-direct destination is never a
            # managed guest DM. Existing contacts keep the old direct handler.
            if raw_from.endswith(("@g.us", "@broadcast", "@newsletter")):
                return False
            if chat_id is not None and not _bounded_string(chat_id, MAX_ID_LENGTH):
                return False
            destination = payload.get("to")
            if destination is not None and (
                not isinstance(destination, str)
                or destination.endswith(("@g.us", "@broadcast", "@newsletter"))
            ):
                return False
            if await self._channel.async_resolve_chat(raw_from) is not None or (
                chat_id is not None
                and await self._channel.async_resolve_chat(chat_id) is not None
            ):
                return False
            sender_alias = raw_from
            conversation_id = None
            conversation_type = "direct"

        # Membership association must use the message's own timestamp. The
        # webhook delivery timestamp cannot prove which stay it belongs to.
        occurred_seconds = _event_timestamp(payload.get("timestamp"), None)
        if occurred_seconds is None:
            return False
        now = time.time()
        if (
            occurred_seconds < now - MAX_EVENT_AGE_SECONDS
            or occurred_seconds > now + MAX_FUTURE_SKEW_SECONDS
        ):
            return False
        member = self._guests.resolve_member(sender_alias, occurred_at=occurred_seconds)
        if member is None:
            return False
        if self._refresh_membership is not None:
            # Re-read the account, exact group, safeguards, and roster before
            # assigning an inbound message to a stay. A cached roster must not
            # authorize traffic after a guest leaves or the bot is re-paired.
            refreshed = await self._refresh_membership()
            if not refreshed.get("ready"):
                return False
            member = self._guests.resolve_member(
                sender_alias, occurred_at=occurred_seconds
            )
            if member is None:
                return False
        if is_group and raw_from != group_jid:
            from_member = self._guests.resolve_member(
                raw_from, occurred_at=occurred_seconds
            )
            if (
                from_member is None
                or from_member["membership_id"] != member["membership_id"]
            ):
                return False
        if not is_group:
            for participant_alias in (payload.get("participant"), chat_id):
                if participant_alias is None:
                    continue
                correlated = self._guests.resolve_member(
                    participant_alias, occurred_at=occurred_seconds
                )
                if (
                    correlated is None
                    or correlated["membership_id"] != member["membership_id"]
                ):
                    return False
            conversation_id = member["direct_conversation_id"]
        if not isinstance(conversation_id, str):
            return False

        media_participant = sender_alias if is_group else payload.get("participant")
        media_route_binding = (
            self._media_manager.capture_observation_binding(
                conversation_id, raw_participant=media_participant
            )
            if self._media_manager is not None
            else None
        )

        sender: dict[str, str] = {
            "participant_id": member["participant_id"],
            "direct_conversation_id": member["direct_conversation_id"],
        }
        if is_group:
            contact = await self._channel.async_resolve_chat(sender_alias)
            if contact is not None:
                # A configured contact's PN must itself be a verified alias of
                # this same registry membership. Names are never identity proof.
                try:
                    contact_jid = recipient_chat_id(contact.recipient)
                except ValueError:
                    contact_jid = None
                contact_member = (
                    self._guests.resolve_member(
                        contact_jid, occurred_at=occurred_seconds
                    )
                    if contact_jid is not None
                    else None
                )
                if (
                    contact_member is not None
                    and contact_member["membership_id"] == member["membership_id"]
                ):
                    sender.update(contact.event_sender())

        raw_message_id = payload.get("id")
        if not _bounded_string(raw_message_id, MAX_ID_LENGTH):
            return False
        raw_event_id = _event_identity(data, payload)
        if raw_event_id is None:
            return False

        if event == "message":
            detail = self._detail_parser._message_detail(payload)
        else:
            detail = await self._detail_parser._reaction_detail(
                payload, conversation_id
            )
        if detail is None:
            return False
        if event == "message":
            detail["message"]["id"] = await self._channel.async_remember_message(
                raw_message_id, conversation_id
            )
        if not await self._channel.async_claim_event(raw_event_id):
            return False

        if event == "message" and payload.get("hasMedia") and self._media_manager:
            detail["message"]["attachments"] = await self._media_manager.async_observe(
                payload,
                detail["message"]["id"],
                conversation_id,
                raw_chat_id=group_jid if is_group else (chat_id or raw_from),
                raw_participant=media_participant,
                route_binding=media_route_binding,
                participant_id=member["participant_id"],
            )

        event_data = {
            "schema_version": CHANNEL_SCHEMA_VERSION,
            "event_id": self._channel.event_token(raw_event_id),
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
            "conversation_id": conversation_id,
            "conversation_type": conversation_type,
            "occurred_at": datetime.fromtimestamp(occurred_seconds, tz=UTC).isoformat(),
            "group": {"group_id": snapshot["group_id"], "purpose": "guests"},
            "sender": sender,
            "membership": {
                "membership_id": member["membership_id"],
                "kind": member["kind"],
                "status": member["status"],
                "whatsapp_role": member["whatsapp_role"],
                "direct_conversation_id": member["direct_conversation_id"],
            },
            **detail,
        }
        # HA callback listeners can run synchronously and mutate event data.
        # Keep command recognition on the private, verified original.
        self._hass.bus.async_fire(EVENT_WAHA_WHATSAPP, deepcopy(event_data))
        if self._command_registry is not None:
            try:
                self._command_registry.publish_from_message(event_data)
            except Exception:
                _LOGGER.exception("WAHA guest command recognition failed")
        return True
