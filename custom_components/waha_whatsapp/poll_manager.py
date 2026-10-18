"""Authenticated WAHA poll webhooks and Home Assistant action events."""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections import Counter
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import Context, HomeAssistant

from .api import WahaClient, WahaError
from .const import (
    CHANNEL_SCHEMA_VERSION,
    EVENT_MOBILE_APP_NOTIFICATION_ACTION,
    EVENT_WAHA_WHATSAPP,
)
from .correlated import DurableStore
from .polls import (
    PendingPoll,
    PollOption,
    PollRegistry,
    PollVote,
    PollVoteRejectionReason,
    canonical_message_id,
    is_lid_chat_id,
    lid_mapping_id,
    parse_poll_vote_with_reason,
    resolve_vote_lids,
    verify_hmac_sha512,
)

_LOGGER = logging.getLogger(__name__)

if TYPE_CHECKING:
    from .channel_registry import ChannelRegistry

STORAGE_VERSION = 1
PERSON_USER_ID_ATTRIBUTE = "user_id"


class WahaPollManager:
    """Persist outgoing polls and translate stable votes to HA events."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        client: WahaClient,
        webhook_secret: str,
        channel_registry: ChannelRegistry,
    ) -> None:
        """Initialize one config entry's actionable poll manager."""
        self._hass = hass
        self._entry = entry
        self._client = client
        self._session_name = client.session_name
        self._webhook_secret = webhook_secret
        self._channel_registry = channel_registry
        self._registry = PollRegistry()
        self._store = DurableStore(
            hass,
            STORAGE_VERSION,
            f"waha_whatsapp.polls.{entry.entry_id}",
            max_storage_bytes=8 * 1024 * 1024,
        )
        self._timers: dict[str, asyncio.TimerHandle] = {}
        self.failed_vote_count = 0
        self._rejected_vote_counts: Counter[PollVoteRejectionReason] = Counter()

    @property
    def pending_count(self) -> int:
        """Return the number of outbound polls still awaiting a stable choice."""
        return len(self._registry.pending_polls())

    @property
    def rejected_vote_count(self) -> int:
        """Return the number of authenticated votes rejected after parsing."""
        return sum(self._rejected_vote_counts.values())

    @property
    def rejected_vote_reasons(self) -> dict[str, int]:
        """Return non-sensitive rejection counters for diagnostics."""
        return {
            reason.value: count
            for reason, count in sorted(
                self._rejected_vote_counts.items(), key=lambda item: item[0].value
            )
        }

    async def async_start(self) -> None:
        """Restore pending polls and their vote-settling timers."""
        self._registry.load(await self._store.async_load())
        for pending in self._registry.pending_polls():
            if (
                pending.commit_at is not None
                and pending.latest_vote_timestamp is not None
                and pending.selected_title is not None
            ):
                self._schedule_commit(
                    pending.message_id,
                    pending.latest_vote_timestamp,
                    pending.commit_at,
                )

    def stop(self) -> None:
        """Cancel timers when the config entry unloads."""
        for timer in self._timers.values():
            timer.cancel()
        self._timers.clear()

    async def async_register_poll(
        self,
        message_id: str,
        chat_id: str,
        options: tuple[PollOption, ...],
        settle_seconds: float,
        person_entity_id: str | None = None,
        *,
        origin: str = "automation",
        client_request_id: str | None = None,
        emit_legacy_action: bool = True,
    ) -> None:
        """Persist the correlation data for one outbound actionable poll."""
        conversation_id = None
        try:
            contact = await self._channel_registry.async_resolve_chat(chat_id)
            if contact is not None:
                conversation_id = contact.conversation_id
        except Exception:
            # Channel metadata must not disable the existing poll action path.
            _LOGGER.exception("Could not track WAHA poll conversation")
        self._registry.register(
            message_id,
            chat_id,
            options,
            settle_seconds,
            person_entity_id,
            conversation_id,
            origin=origin,
            client_request_id=client_request_id,
            emit_legacy_action=emit_legacy_action,
        )
        await self._async_save()

    def verify_signature(self, raw_body: bytes, signature: str | None) -> bool:
        """Verify WAHA's SHA-512 HMAC over the unmodified request body."""
        return verify_hmac_sha512(self._webhook_secret, raw_body, signature)

    async def async_handle_payload(self, data: Mapping[str, Any]) -> None:
        """Process one authenticated WAHA poll event."""
        event = data.get("event")
        if event == "poll.vote.failed":
            if data.get("session") == self._session_name:
                self.failed_vote_count += 1
                _LOGGER.warning(
                    "WAHA could not decrypt a poll vote; no Home Assistant "
                    "action was fired"
                )
            return

        vote, rejection_reason = parse_poll_vote_with_reason(data, self._session_name)
        if rejection_reason is not None:
            self._record_rejected_vote(rejection_reason)
            return

        assert vote is not None
        pending = self._registry.pending(vote.message_id)
        if pending is None:
            self._record_rejected_vote(PollVoteRejectionReason.UNKNOWN_POLL)
            return

        vote = await self._async_resolve_vote_lids(vote)
        if vote is None:
            self._record_rejected_vote(PollVoteRejectionReason.LID_RESOLUTION_FAILED)
            return

        rejection_reason = self._registry.apply_vote_with_reason(vote, time.time())
        if rejection_reason is not None:
            self._record_rejected_vote(rejection_reason)
            return

        message_id = canonical_message_id(vote.message_id)
        previous = self._timers.pop(message_id, None)
        if previous is not None:
            previous.cancel()

        pending = self._registry.pending(message_id)
        if pending is not None and pending.commit_at is not None:
            self._schedule_commit(message_id, vote.timestamp, pending.commit_at)
        await self._async_save()

    @staticmethod
    def decode_payload(raw_body: bytes) -> Mapping[str, Any] | None:
        """Decode one JSON object without accepting other JSON top-level types."""
        try:
            data = json.loads(raw_body)
        except UnicodeDecodeError, json.JSONDecodeError:
            return None
        return data if isinstance(data, Mapping) else None

    def _schedule_commit(
        self, message_id: str, vote_timestamp: float, commit_at: float
    ) -> None:
        """Schedule one vote after its correction window closes."""
        previous = self._timers.pop(message_id, None)
        if previous is not None:
            previous.cancel()
        self._timers[message_id] = self._hass.loop.call_later(
            max(0.0, commit_at - time.time()),
            self._create_commit_task,
            message_id,
            vote_timestamp,
        )

    def _create_commit_task(self, message_id: str, vote_timestamp: float) -> None:
        """Move the timer callback into an async Home Assistant task."""
        self._timers.pop(message_id, None)
        self._entry.async_create_task(
            self._hass,
            self._async_commit(message_id, vote_timestamp),
            "settle WAHA poll action",
        )

    async def _async_commit(self, message_id: str, vote_timestamp: float) -> None:
        """Publish a settled channel selection and preserve the legacy action."""
        pending = self._registry.pending(message_id)
        person_entity_id = pending.person_entity_id if pending is not None else None
        committed, action = self._registry.commit(
            message_id, vote_timestamp, time.time()
        )
        if not committed:
            pending = self._registry.pending(message_id)
            if (
                pending is not None
                and pending.latest_vote_timestamp == vote_timestamp
                and pending.commit_at is not None
            ):
                self._schedule_commit(message_id, vote_timestamp, pending.commit_at)
            return

        selection = None
        if pending is not None and pending.origin == "application":
            selection = self._settled_selection(pending, vote_timestamp, action)
            if selection is not None:
                self._registry.remember_selection(selection)
        try:
            await self._async_save()
        except Exception:
            if pending is not None and pending.origin == "application":
                # The last durable snapshot still contains the uncommitted vote.
                # Restore it in memory too; never announce a non-durable action.
                self._registry.restore_uncommitted(pending, selection)
                _LOGGER.warning(
                    "Application poll decision not published: persistence failed"
                )
            raise
        getattr(self, "_persistence_retries", {}).pop(message_id, None)
        context = await self._async_person_context(person_entity_id)
        if action is not None and (pending is None or pending.emit_legacy_action):
            self._hass.bus.async_fire(
                EVENT_MOBILE_APP_NOTIFICATION_ACTION,
                {"action": action},
                context=context,
            )
        if pending is not None:
            try:
                if selection is not None:
                    self._hass.bus.async_fire(
                        EVENT_WAHA_WHATSAPP, selection, context=context
                    )
                else:
                    self._publish_settled_selection(
                        pending, vote_timestamp, action, context
                    )
            except Exception:
                # Channel observation must not undo a successful legacy action.
                _LOGGER.exception("Could not publish settled WAHA poll channel event")

    def _publish_settled_selection(
        self,
        pending: PendingPoll,
        vote_timestamp: float,
        action: str | None,
        context: Context | None,
    ) -> None:
        """Emit one privacy-safe channel event for a configured direct chat."""
        payload = self._settled_selection(pending, vote_timestamp, action)
        if payload is not None:
            self._hass.bus.async_fire(EVENT_WAHA_WHATSAPP, payload, context=context)

    def _settled_selection(
        self, pending, vote_timestamp, action
    ) -> dict[str, Any] | None:
        if pending.conversation_id is None:
            # Polls stored before v1.4.1 have no safe contact association.
            return
        contact = self._channel_registry.resolve_conversation(pending.conversation_id)
        if contact is None:
            return
        event_id = self._channel_registry.event_token(
            f"poll.selection_settled:{pending.message_id}:{vote_timestamp}"
        )
        return {
            "schema_version": CHANNEL_SCHEMA_VERSION,
            "event_id": event_id,
            "type": "poll.selection_settled",
            "config_entry_id": self._entry.entry_id,
            "conversation_id": contact.conversation_id,
            "conversation_type": "direct",
            "occurred_at": datetime.now(UTC).isoformat(),
            "sender": contact.event_sender(),
            "poll": {
                "message_id": self._channel_registry.message_token(pending.message_id),
                "selected_option": (
                    None if pending.origin == "application" else pending.selected_title
                ),
                "action_id": action,
            },
            **(
                {
                    "origin": pending.origin,
                    "client_request_id": pending.client_request_id,
                    "vote_occurred_at": datetime.fromtimestamp(
                        vote_timestamp, UTC
                    ).isoformat(),
                }
                if pending.origin == "application"
                else {}
            ),
        }

    def selections(self, conversation_id, after_sequence=0, limit=100):
        """Read the bounded application receipt journal for one current contact."""
        if self._channel_registry.resolve_conversation(conversation_id) is None:
            return {"events": [], "has_more": False, "next_sequence": after_sequence}
        return self._registry.selections(conversation_id, after_sequence, limit)

    async def _async_save(self) -> None:
        """Write correlation state immediately so restarts do not lose votes."""
        if not hasattr(self, "_save_lock"):
            self._save_lock = asyncio.Lock()
        async with self._save_lock:
            await self._store.async_save(self._registry.as_dict())

    async def _async_resolve_vote_lids(self, vote: PollVote) -> PollVote | None:
        """Resolve alternate direct-chat identities through WAHA itself."""
        lids = {
            lid_mapping_id(chat_id)
            for chat_id in (vote.sender, vote.poll_chat_id)
            if is_lid_chat_id(chat_id)
        }
        if not lids:
            return vote

        mappings: dict[str, str] = {}
        try:
            for lid in lids:
                phone_chat_id = await self._client.async_resolve_lid(lid)
                if phone_chat_id is None:
                    return None
                mappings[lid.casefold()] = phone_chat_id
        except WahaError:
            return None
        return resolve_vote_lids(vote, mappings)

    async def _async_person_context(
        self, person_entity_id: str | None
    ) -> Context | None:
        """Resolve a linked Person's current active user for attribution."""
        if person_entity_id is None:
            return None
        person_state = self._hass.states.get(person_entity_id)
        if person_state is None:
            return None
        user_id = person_state.attributes.get(PERSON_USER_ID_ATTRIBUTE)
        if not isinstance(user_id, str) or not user_id:
            return None
        user = await self._hass.auth.async_get_user(user_id)
        if user is None or not user.is_active:
            return None
        return Context(user_id=user.id)

    def _record_rejected_vote(self, reason: PollVoteRejectionReason) -> None:
        """Record and log a rejection without identifiers or payload content."""
        self._rejected_vote_counts[reason] += 1
        _LOGGER.warning(
            "Rejected authenticated WAHA poll vote (%s); no Home Assistant "
            "action was fired",
            reason.value,
        )
