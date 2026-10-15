"""Private, bounded routing and correlation state for the WhatsApp channel."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import re
import time
from collections.abc import Callable, Mapping
from copy import deepcopy
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from .api import WahaClient, WahaError
from .const import (
    CONF_PERSON_ENTITY_ID,
    CONF_RECIPIENT,
    CONF_WEBHOOK_SECRET,
    DOMAIN,
    SUBENTRY_TYPE_RECIPIENT,
)
from .correlated import DurableStore
from .helpers import normalize_chat_id, normalize_recipient, recipient_chat_id
from .polls import canonical_message_id, is_lid_chat_id, lid_mapping_id

if TYPE_CHECKING:
    from homeassistant.config_entries import ConfigEntry, ConfigSubentry
    from homeassistant.core import HomeAssistant

STORAGE_VERSION = 1
MESSAGE_TTL_SECONDS = 7 * 24 * 60 * 60
EVENT_TTL_SECONDS = 24 * 60 * 60
MAX_TRACKED_MESSAGES = 2048
MAX_CLAIMED_EVENTS = 4096
MAX_CHANNEL_STORAGE_BYTES = 8 * 1024 * 1024
_DIRECT_NETWORK_JID_RE = re.compile(r"^([0-9]{7,15})@s\.whatsapp\.net$", re.I)


@dataclass(frozen=True, slots=True)
class ChannelContact:
    """A configured direct-chat contact; ``recipient`` must stay private."""

    conversation_id: str
    subentry_id: str
    recipient: str
    notify_entity_id: str | None
    person_entity_id: str | None

    def event_sender(self) -> dict[str, str]:
        """Expose the same non-sensitive contact identity on channel events."""
        sender = {"contact_id": self.subentry_id}
        if self.notify_entity_id:
            sender["notify_entity_id"] = self.notify_entity_id
        if self.person_entity_id:
            sender["person_entity_id"] = self.person_entity_id
        return sender


@dataclass(slots=True)
class _TrackedMessage:
    """Short-lived mapping for quoted replies and reaction correlation."""

    raw_id: str
    conversation_id: str
    recipient_fingerprint: str
    expires_at: float


class _VolatileStore:
    """Keep correlation state only for the lifetime of this registry."""

    def __init__(self) -> None:
        self._data: dict[str, Any] | None = None

    async def async_load(self) -> dict[str, Any] | None:
        """Return an independent snapshot of the in-memory state."""
        return deepcopy(self._data)

    async def async_save(self, data: dict[str, Any]) -> None:
        """Save without touching Home Assistant's on-disk Store."""
        self._data = deepcopy(data)


class _ChannelWriterSlot:
    """Serialize private channel snapshots across entry runtime replacement."""

    def __init__(self) -> None:
        self.lock = asyncio.Lock()
        self.owner: ChannelRegistry | None = None


class ChannelRegistry:
    """Resolve configured contacts and persist bounded opaque message state."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        client: WahaClient,
        *,
        store: Any | None = None,
    ) -> None:
        """Initialize routing using the entry's stable private webhook secret."""
        self._hass = hass
        self._entry = entry
        self._client = client
        secret = entry.data[CONF_WEBHOOK_SECRET]
        if not isinstance(secret, str) or not secret:
            raise ValueError("WAHA channel secret is missing")
        self._secret = secret.encode("utf-8")
        if store is None:
            store = DurableStore(
                hass,
                STORAGE_VERSION,
                f"waha_whatsapp.channel.{entry.entry_id}",
                max_storage_bytes=MAX_CHANNEL_STORAGE_BYTES,
            )
        self._store = store
        if not hasattr(hass, "data"):
            hass.data = {}
        slots = hass.data.setdefault("waha_whatsapp.channel_writers", {})
        self._writer_slot = slots.setdefault(entry.entry_id, _ChannelWriterSlot())
        self._stopping = False
        self._pending_message_tokens: set[str] = set()
        self.persistence_available = True
        self._lock = asyncio.Lock()
        self._messages: dict[str, _TrackedMessage] = {}
        self._events: dict[str, float] = {}
        self._extra_route_resolver: Callable[[str], str | None] | None = None

    def set_extra_route_resolver(
        self, resolver: Callable[[str], str | None] | None
    ) -> None:
        """Register confirmed managed-group routes for message correlation.

        The resolver must return no destination when the group is disabled,
        unverified, or a guest membership has ended. It never changes direct
        configured-contact lookup or its independent eligibility.
        """
        self._extra_route_resolver = resolver

    def use_volatile_storage(self) -> None:
        """Discard untrusted restored state and avoid writing the disk store."""
        self._messages.clear()
        self._events.clear()
        self._store = _VolatileStore()
        self.persistence_available = False

    async def async_start(self) -> None:
        """Drain the previous runtime before reading its committed snapshot."""
        async with self._writer_slot.lock:
            if self._stopping:
                return
            previous = self._writer_slot.owner
            if previous is not None and previous is not self:
                await previous.async_stop()
            if self._stopping:
                return
            self._writer_slot.owner = self
            async with self._lock:
                if self._stopping:
                    return
                data = await self._store.async_load()
                if not self._stopping:
                    self._restore(data)

    def _restore(self, data: object) -> None:
        """Restore bounded quote and callback mappings from one loaded snapshot."""
        if not isinstance(data, Mapping):
            return
        now = time.time()
        messages = data.get("messages")
        if isinstance(messages, list):
            for item in messages[-MAX_TRACKED_MESSAGES:]:
                if not isinstance(item, Mapping):
                    continue
                token = item.get("token")
                raw_id = item.get("raw_id")
                conversation_id = item.get("conversation_id")
                recipient_fingerprint = item.get("recipient_fingerprint")
                expires_at = item.get("expires_at")
                destination = self._conversation_destination(conversation_id)
                # A managed group route is deliberately unconfirmed at
                # startup. Keep its opaque, bounded quote token dormant until
                # a fresh roster verifies the route; resolve_message checks
                # the destination fingerprint again before exposing the ID.
                pending_managed_route = (
                    isinstance(conversation_id, str)
                    and re.fullmatch(
                        r"(?:group_conversation|guest_direct|general_group)_[0-9a-f]{32}",
                        conversation_id,
                    )
                    is not None
                )
                if (
                    isinstance(token, str)
                    and isinstance(raw_id, str)
                    and bool(raw_id)
                    and isinstance(conversation_id, str)
                    and isinstance(recipient_fingerprint, str)
                    and isinstance(expires_at, (int, float))
                    and expires_at > now
                    and token == self.message_token(raw_id)
                    and re.fullmatch(r"rec_[0-9a-f]{32}", recipient_fingerprint)
                    is not None
                    and (
                        recipient_fingerprint
                        == self._recipient_fingerprint(destination)
                        if destination is not None
                        else pending_managed_route
                    )
                ):
                    self._messages[token] = _TrackedMessage(
                        raw_id,
                        conversation_id,
                        recipient_fingerprint,
                        float(expires_at),
                    )
        events = data.get("events")
        if isinstance(events, list):
            for item in events[-MAX_CLAIMED_EVENTS:]:
                if not isinstance(item, Mapping):
                    continue
                token = item.get("token")
                seen_at = item.get("seen_at")
                if (
                    isinstance(token, str)
                    and token.startswith("evt_")
                    and isinstance(seen_at, (int, float))
                    and now - EVENT_TTL_SECONDS < seen_at <= now
                ):
                    self._events[token] = float(seen_at)
        self._prune(now)

    def stop(self) -> None:
        """Immediately fence new mutations, including already queued callbacks."""
        self._stopping = True
        self.persistence_available = False

    async def async_stop(self) -> None:
        """Fence synchronously, then drain the current physical snapshot write.

        Cancellation keeps the fence and writer ownership intact. Replacement
        startup also waits on this lock before loading or saving any snapshot.
        """
        self.stop()
        async with self._lock:
            pass

    def _require_writable(self) -> None:
        if self._stopping:
            raise RuntimeError("WAHA channel registry is stopped")

    async def async_resolve_chat(self, chat_id: str) -> ChannelContact | None:
        """Map only a configured direct phone/LID chat, failing closed."""
        if not isinstance(chat_id, str) or not chat_id:
            return None
        if is_lid_chat_id(chat_id):
            try:
                mapped = await self._client.async_resolve_lid(lid_mapping_id(chat_id))
            except WahaError:
                return None
            if mapped is None:
                return None
            chat_id = mapped
        elif direct_jid := _DIRECT_NETWORK_JID_RE.fullmatch(chat_id):
            chat_id = f"{direct_jid.group(1)}@c.us"
        try:
            normalized_chat = normalize_chat_id(chat_id)
        except ValueError:
            return None
        matches = [
            contact
            for subentry in self._entry.subentries.values()
            if (contact := self._contact(subentry)) is not None
            and recipient_chat_id(contact.recipient) == normalized_chat
        ]
        return matches[0] if len(matches) == 1 else None

    def resolve_conversation(self, conversation_id: str) -> ChannelContact | None:
        """Resolve an opaque routing handle only while its contact exists."""
        if not isinstance(conversation_id, str) or not conversation_id:
            return None
        for subentry in self._entry.subentries.values():
            contact = self._contact(subentry)
            if contact is not None and contact.conversation_id == conversation_id:
                # Config flow prevents duplicate phone numbers, but corrupt or
                # hand-edited config must never allow an ambiguous reply target.
                matches = sum(
                    1
                    for other in self._entry.subentries.values()
                    if (candidate := self._contact(other)) is not None
                    and candidate.recipient == contact.recipient
                )
                return contact if matches == 1 else None
        return None

    def _conversation_destination(self, conversation_id: str) -> str | None:
        """Resolve one currently allowed route solely for message ownership."""
        contact = self.resolve_conversation(conversation_id)
        if contact is not None:
            return contact.recipient
        if self._extra_route_resolver is not None:
            return self._extra_route_resolver(conversation_id)
        return None

    def message_token(self, raw_id: str) -> str:
        """Create one deterministic public ID across WAHA serialized variants."""
        if not isinstance(raw_id, str) or not raw_id:
            raise ValueError("WAHA message ID is missing")
        return self._token("message", canonical_message_id(raw_id), "msg")

    def opaque_id(self, purpose: str, value: str, prefix: str) -> str:
        """Derive a connection-scoped public identifier without exposing routing."""
        return self._token(purpose, value, prefix)

    def event_token(self, raw_event_id: str) -> str:
        """Create a stable public event ID without exposing the WAHA ID."""
        if not isinstance(raw_event_id, str) or not raw_event_id:
            raise ValueError("WAHA event ID is missing")
        return self._token("event", raw_event_id, "evt")

    async def async_remember_message(self, raw_id: str, conversation_id: str) -> str:
        """Keep a WAHA ID privately for bounded quoted replies/reactions."""
        self._require_writable()
        destination = self._conversation_destination(conversation_id)
        if destination is None:
            raise ValueError("Unknown WAHA conversation")
        token = self.message_token(raw_id)
        async with self._lock:
            self._require_writable()
            previous_messages = self._messages.copy()
            previous_events = self._events.copy()
            self._pending_message_tokens.add(token)
            try:
                now = time.time()
                self._prune(now)
                self._messages[token] = _TrackedMessage(
                    raw_id,
                    conversation_id,
                    self._recipient_fingerprint(destination),
                    now + MESSAGE_TTL_SECONDS,
                )
                self._prune(now)
                await self._save()
            except Exception, asyncio.CancelledError:
                self._messages = previous_messages
                self._events = previous_events
                raise
            finally:
                self._pending_message_tokens.discard(token)
        return token

    def resolve_message(self, token: str, conversation_id: str) -> str | None:
        """Return a tracked WAHA ID only for the same configured conversation."""
        destination = self._conversation_destination(conversation_id)
        if destination is None:
            return None
        item = self._messages.get(token)
        if (
            item is None
            or token in self._pending_message_tokens
            or item.conversation_id != conversation_id
            or item.recipient_fingerprint != self._recipient_fingerprint(destination)
            or item.expires_at <= time.time()
        ):
            return None
        return item.raw_id

    async def async_message_known(self, raw_id: str, conversation_id: str) -> bool:
        """Tell event consumers whether a reaction target was tracked here."""
        token = self.message_token(raw_id)
        return self.resolve_message(token, conversation_id) is not None

    async def async_claim_event(self, raw_event_id: str) -> bool:
        """Atomically suppress a retried webhook within a bounded window."""
        self._require_writable()
        token = self.event_token(raw_event_id)
        async with self._lock:
            self._require_writable()
            previous_messages = self._messages.copy()
            previous_events = self._events.copy()
            try:
                now = time.time()
                self._prune(now)
                if token in self._events:
                    return False
                self._events[token] = now
                self._prune(now)
                await self._save()
                return True
            except Exception, asyncio.CancelledError:
                self._messages = previous_messages
                self._events = previous_events
                raise

    def _conversation_id(self, subentry_id: str, recipient: str) -> str:
        # A phone edit changes the destination. Old automation events must not
        # silently route a delayed private response to the replacement number.
        return self._token("conversation", f"{subentry_id}\0{recipient}", "direct")

    def _recipient_fingerprint(self, recipient: str) -> str:
        return self._token("recipient", recipient, "rec")

    def _token(self, purpose: str, value: str, prefix: str) -> str:
        material = (
            f"{purpose}\0{self._entry.entry_id}\0{self._client.session_name}\0{value}"
        )
        digest = hmac.new(
            self._secret, material.encode("utf-8"), hashlib.sha256
        ).hexdigest()[:32]
        return f"{prefix}_{digest}"

    def _contact(self, subentry: ConfigSubentry) -> ChannelContact | None:
        if subentry.subentry_type != SUBENTRY_TYPE_RECIPIENT:
            return None
        recipient = subentry.data.get(CONF_RECIPIENT)
        if not isinstance(recipient, str):
            return None
        try:
            recipient = normalize_recipient(recipient)
        except ValueError:
            return None
        person_entity_id = subentry.data.get(CONF_PERSON_ENTITY_ID)
        return ChannelContact(
            conversation_id=self._conversation_id(subentry.subentry_id, recipient),
            subentry_id=subentry.subentry_id,
            recipient=recipient,
            notify_entity_id=_notify_entity_id(
                self._hass, self._entry.entry_id, subentry.subentry_id
            ),
            person_entity_id=(
                person_entity_id
                if isinstance(person_entity_id, str) and person_entity_id
                else None
            ),
        )

    def _prune(self, now: float) -> None:
        self._messages = {
            token: item
            for token, item in self._messages.items()
            if item.expires_at > now
        }
        if len(self._messages) > MAX_TRACKED_MESSAGES:
            self._messages = dict(
                sorted(self._messages.items(), key=lambda pair: pair[1].expires_at)[
                    -MAX_TRACKED_MESSAGES:
                ]
            )
        self._events = {
            token: seen_at
            for token, seen_at in self._events.items()
            if seen_at > now - EVENT_TTL_SECONDS
        }
        if len(self._events) > MAX_CLAIMED_EVENTS:
            self._events = dict(
                sorted(self._events.items(), key=lambda pair: pair[1])[
                    -MAX_CLAIMED_EVENTS:
                ]
            )

    async def _save(self) -> None:
        await self._store.async_save(
            {
                "messages": [
                    {
                        "token": token,
                        "raw_id": item.raw_id,
                        "conversation_id": item.conversation_id,
                        "recipient_fingerprint": item.recipient_fingerprint,
                        "expires_at": item.expires_at,
                    }
                    for token, item in self._messages.items()
                ],
                "events": [
                    {"token": token, "seen_at": seen_at}
                    for token, seen_at in self._events.items()
                ],
            }
        )


def _notify_entity_id(
    hass: HomeAssistant, entry_id: str, subentry_id: str
) -> str | None:
    """Find the individual notify entity without deriving it from its title."""
    from homeassistant.helpers import entity_registry as er

    registry = er.async_get(hass)
    entities = [
        entity
        for entity in er.async_entries_for_config_entry(registry, entry_id)
        if entity.domain == "notify"
        and entity.platform == DOMAIN
        and entity.config_subentry_id == subentry_id
    ]
    return entities[0].entity_id if len(entities) == 1 else None
