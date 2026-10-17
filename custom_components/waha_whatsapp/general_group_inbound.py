"""Normalize authenticated explicit-group messages without command execution."""

from __future__ import annotations

import time
from collections.abc import Mapping
from copy import deepcopy
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from .api import WahaError
from .const import CHANNEL_SCHEMA_VERSION, CONF_SESSION, EVENT_WAHA_WHATSAPP
from .general_groups import GeneralGroupError
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
    from .general_groups import (
        GeneralGroupDestination,
        GeneralGroupManager,
        VerifiedGeneralGroup,
    )


class WahaGeneralGroupInboundManager:
    """Publish only enabled exact groups after fresh account and membership proof."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        manager: GeneralGroupManager,
        channel_registry: ChannelRegistry,
        media_manager: Any | None = None,
    ) -> None:
        self._hass = hass
        self._entry = entry
        self._groups = manager
        self._channel = channel_registry
        self._media_manager = media_manager
        self._parser = WahaInboundManager(hass, entry, channel_registry)

    async def async_handle_payload(self, data: Mapping[str, Any]) -> bool:
        """Handle an HMAC-verified envelope. No Person context or command handling."""
        event = data.get("event")
        payload = data.get("payload")
        if (
            event not in ("message", "message.reaction")
            or data.get("session") != self._entry.data[CONF_SESSION]
            or not isinstance(payload, Mapping)
            or payload.get("fromMe") is not False
        ):
            return False
        raw_from = payload.get("from")
        chat = payload.get("chatId", raw_from)
        if not _bounded_string(chat, MAX_ID_LENGTH):
            return False
        destination = self._groups.resolve_group(chat)
        if destination is None or not _bounded_string(raw_from, MAX_ID_LENGTH):
            return False
        if raw_from.endswith("@g.us") and raw_from != destination.group_jid:
            return False
        occurred = _event_timestamp(payload.get("timestamp"), None)
        now = time.time()
        if occurred is None or not (
            now - MAX_EVENT_AGE_SECONDS <= occurred <= now + MAX_FUTURE_SKEW_SECONDS
        ):
            return False
        raw_id = payload.get("id")
        event_id = _event_identity(data, payload)
        if not _bounded_string(raw_id, MAX_ID_LENGTH) or event_id is None:
            return False
        try:
            proof = await self._groups.async_verify(destination)
        except WahaError, GeneralGroupError:
            return False
        # GOWS' `to` for an inbound group message is its participant, not
        # the account or group. Correlate it when supplied, without guessing.
        sender_aliases = [payload.get("participant"), payload.get("to")]
        if raw_from != destination.group_jid:
            sender_aliases.append(raw_from)
        identities = [
            proof.identity(alias) for alias in sender_aliases if alias is not None
        ]
        resolved = {identity for identity in identities if identity is not None}
        if len(resolved) > 1:
            return False
        identity = (
            next(iter(resolved)) if identities and None not in identities else None
        )
        sender = {
            "participant_id": (
                self._groups.participant_id(destination, identity) if identity else None
            ),
            "identity_status": "verified" if identity else "unresolved",
        }
        media_participant = (
            payload.get("participant")
            or payload.get("to")
            or (raw_from if raw_from != destination.group_jid else None)
        )
        media_route_binding = (
            self._media_manager.capture_observation_binding(
                destination.conversation_id, raw_participant=media_participant
            )
            if self._media_manager is not None
            else None
        )
        if event == "message":
            detail = self._parser._message_detail(payload)
            if detail is None:
                return False
            message = detail["message"]
            # A raw cross-chat quote never becomes a usable group quote token.
            quote = message.get("in_reply_to")
            if (
                quote
                and self._channel.resolve_message(quote, destination.conversation_id)
                is None
            ):
                message["in_reply_to"] = None
            message["mentions"] = self._mentions(payload, destination, proof)
            message["id"] = await self._channel.async_remember_message(
                raw_id, destination.conversation_id
            )
        else:
            detail = await self._parser._reaction_detail(
                payload, destination.conversation_id
            )
            if detail is None:
                return False
        if not await self._channel.async_claim_event(event_id):
            return False
        if event == "message" and payload.get("hasMedia") and self._media_manager:
            detail["message"]["attachments"] = await self._media_manager.async_observe(
                payload,
                detail["message"]["id"],
                destination.conversation_id,
                raw_chat_id=destination.group_jid,
                raw_participant=media_participant,
                route_binding=media_route_binding,
                participant_id=sender["participant_id"],
            )

        event_data = {
            "schema_version": CHANNEL_SCHEMA_VERSION,
            "event_id": self._channel.event_token(event_id),
            "type": (
                "message.received"
                if event == "message"
                else "reaction.added"
                if detail["reaction"]["emoji"]
                else "reaction.removed"
            ),
            "config_entry_id": self._entry.entry_id,
            "conversation_id": destination.conversation_id,
            "conversation_type": "group",
            "occurred_at": datetime.fromtimestamp(occurred, tz=UTC).isoformat(),
            "group": {"purpose": "general", "contact_id": destination.subentry_id},
            "sender": sender,
            **detail,
        }
        # Storage awaits above can yield to config updates. Revoke publication
        # immediately if this exact route was disabled, removed or rebound.
        if (
            self._groups.resolve_conversation(destination.conversation_id)
            != destination
        ):
            return False
        self._hass.bus.async_fire(EVENT_WAHA_WHATSAPP, deepcopy(event_data))
        return True

    def _mentions(
        self,
        payload: Mapping[str, Any],
        destination: GeneralGroupDestination,
        proof: VerifiedGeneralGroup,
    ) -> dict[str, Any]:
        """Read only known GOWS protobuf context with fully roster-resolved JIDs.

        GOWS retains the Go message in ``_data.Message``; the protobuf JSON
        name is ``contextInfo.mentionedJID``. No top-level normalized mention
        field is currently supplied by GOWS. Missing context is unavailable,
        rather than proof that the account was not mentioned.
        """
        unavailable = {"available": False, "bot": None, "participant_ids": []}
        raw = payload.get("_data")
        raw_message = raw.get("Message") if isinstance(raw, Mapping) else None
        if not isinstance(raw_message, Mapping):
            return unavailable
        contexts = []
        for content_type in (
            "extendedTextMessage",
            "imageMessage",
            "videoMessage",
            "audioMessage",
            "documentMessage",
            "stickerMessage",
        ):
            content = raw_message.get(content_type)
            if isinstance(content, Mapping) and "contextInfo" in content:
                contexts.append(content["contextInfo"])
        if len(contexts) != 1 or not isinstance(contexts[0], Mapping):
            return unavailable
        mentions = contexts[0].get("mentionedJID")
        if not isinstance(mentions, list) or len(mentions) > 1024:
            return unavailable
        identities = [proof.identity(alias) for alias in mentions]
        if any(identity is None for identity in identities):
            return unavailable
        return {
            "available": True,
            "bot": proof.bot_identity in identities,
            "participant_ids": sorted(
                {
                    self._groups.participant_id(destination, identity)
                    for identity in identities
                    if identity is not None
                }
            ),
        }
