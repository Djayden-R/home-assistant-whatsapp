"""Authenticated WAHA poll webhooks and Home Assistant action events."""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections import Counter
from collections.abc import Mapping
from copy import deepcopy
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
RECOVERY_DELAYS = (1, 5, 30)
MAX_RECOVERY_POLLS = 256


class _PollWriterSlot:
    """Drain one entry's previous runtime before a replacement reads its store."""

    def __init__(self) -> None:
        self.lock = asyncio.Lock()
        self.owner: WahaPollManager | None = None


class WahaPollManager:
    """Persist outgoing polls and translate stable votes to HA events."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        client: WahaClient,
        webhook_secret: str,
        channel_registry: ChannelRegistry,
        *,
        store: Any | None = None,
    ) -> None:
        """Initialize one config entry's actionable poll manager."""
        self._hass = hass
        self._entry = entry
        self._client = client
        self._session_name = client.session_name
        self._webhook_secret = webhook_secret
        self._channel_registry = channel_registry
        self._registry = PollRegistry()
        self._store = (
            store
            if store is not None
            else DurableStore(
                hass,
                STORAGE_VERSION,
                f"waha_whatsapp.polls.{entry.entry_id}",
                max_storage_bytes=8 * 1024 * 1024,
            )
        )
        self._lock = asyncio.Lock()
        slots = hass.data.setdefault("waha_whatsapp.poll_writers", {})
        self._writer_slot = slots.setdefault(entry.entry_id, _PollWriterSlot())
        self._stopping = False
        self._timers: dict[str, asyncio.TimerHandle] = {}
        self._timer_tokens: dict[str, object] = {}
        self._recovery: dict[str, tuple[float | None, int]] = {}
        self._failures = Counter()
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

    @property
    def diagnostics(self) -> dict[str, int]:
        """Expose bounded aggregate recovery state without poll identities."""
        return {
            "construction_failures": self._failures["construction"],
            "persistence_failures": self._failures["persistence"],
            "publication_failures": self._failures["publication"],
            "retry_exhausted": self._failures["retry_exhausted"],
            "recovery_pending": len(self._recovery),
            "scheduled_work": len(self._timers),
            "invalid_pending_timestamps": sum(
                pending.invalid_timestamp for pending in self._registry.pending_polls()
            ),
            "invalid_pending_records": self._registry.invalid_pending_records,
        }

    def _require_running(self) -> None:
        if self._stopping or self._writer_slot.owner is not self:
            raise RuntimeError("WAHA poll manager is stopped")

    async def async_start(self) -> None:
        """Drain a replaced writer before restoring pending polls and timers."""
        async with self._writer_slot.lock:
            if self._stopping:
                return
            previous = self._writer_slot.owner
            if previous is not None and previous is not self:
                await previous.async_stop()
            async with self._lock:
                if self._stopping:
                    return
                self._writer_slot.owner = self
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
        """Fence queued callbacks and cancel all bounded recovery deadlines."""
        self._stopping = True
        for timer in self._timers.values():
            timer.cancel()
        self._timers.clear()
        self._timer_tokens.clear()
        self._recovery.clear()

    async def async_stop(self) -> None:
        """Fence immediately, then drain the admitted transaction/physical write."""
        self.stop()
        async with self._lock:
            pass

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
        self._require_running()
        conversation_id = None
        try:
            contact = await self._channel_registry.async_resolve_chat(chat_id)
            if contact is not None:
                conversation_id = contact.conversation_id
        except Exception:
            # Channel metadata must not disable the existing poll action path.
            _LOGGER.warning("Could not track WAHA poll conversation")
        async with self._lock:
            self._require_running()
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
            try:
                cancelled = await self._write_registry(self._registry)
            except Exception:
                self._failure("persistence")
                self._schedule_recovery(canonical_message_id(message_id), None)
                raise
            if cancelled:
                raise asyncio.CancelledError

    def verify_signature(self, raw_body: bytes, signature: str | None) -> bool:
        """Verify WAHA's SHA-512 HMAC over the unmodified request body."""
        return verify_hmac_sha512(self._webhook_secret, raw_body, signature)

    async def async_handle_payload(self, data: Mapping[str, Any]) -> None:
        """Process one authenticated WAHA poll event."""
        if self._stopping or self._writer_slot.owner is not self:
            return
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

        async with self._lock:
            if self._stopping or self._writer_slot.owner is not self:
                return
            pending = self._registry.pending(vote.message_id)
            if (
                pending is not None
                and pending.origin == "application"
                and not self._current_application_route(pending)
            ):
                self._record_rejected_vote(PollVoteRejectionReason.POLL_CHAT_MISMATCH)
                return
            rejection_reason = self._registry.apply_vote_with_reason(vote, time.time())
            if rejection_reason is not None:
                self._record_rejected_vote(rejection_reason)
                return
            message_id = canonical_message_id(vote.message_id)
            self._cancel_work(message_id)
            self._recovery.pop(message_id, None)
            try:
                cancelled = await self._write_registry(self._registry)
            except Exception:
                # Retain the newest accepted answer in memory. A later answer
                # queues behind this transaction and cannot be rolled back.
                self._failure("persistence")
                self._schedule_recovery(message_id, vote.timestamp)
                return
            pending = self._registry.pending(message_id)
            if pending is not None and pending.commit_at is not None:
                self._schedule_commit(message_id, vote.timestamp, pending.commit_at)
            if cancelled:
                raise asyncio.CancelledError

    @staticmethod
    def decode_payload(raw_body: bytes) -> Mapping[str, Any] | None:
        """Decode one JSON object without accepting other JSON top-level types."""
        try:
            data = json.loads(raw_body)
        except UnicodeDecodeError, ValueError:
            return None
        return data if isinstance(data, Mapping) else None

    def _cancel_work(self, message_id: str) -> None:
        previous = self._timers.pop(message_id, None)
        if previous is not None:
            previous.cancel()
        self._timer_tokens.pop(message_id, None)

    def _schedule_commit(
        self,
        message_id: str,
        vote_timestamp: float,
        commit_at: float,
        *,
        recovery: bool = False,
    ) -> None:
        if self._stopping:
            return
        self._cancel_work(message_id)
        token = object()
        self._timer_tokens[message_id] = token
        self._timers[message_id] = self._hass.loop.call_later(
            max(0.0, commit_at - time.time()),
            self._create_commit_task,
            message_id,
            vote_timestamp,
            token,
            recovery,
        )

    def _create_commit_task(
        self,
        message_id: str,
        vote_timestamp: float | None,
        token: object,
        recovery: bool = False,
    ) -> None:
        if self._stopping or self._timer_tokens.get(message_id) is not token:
            return
        self._timers.pop(message_id, None)
        self._timer_tokens.pop(message_id, None)
        self._entry.async_create_task(
            self._hass,
            self._async_recover(message_id, vote_timestamp)
            if recovery
            else self._async_commit(message_id, vote_timestamp),
            "settle WAHA poll action",
        )

    def _failure(self, phase: str) -> None:
        self._failures[phase] += 1
        _LOGGER.warning("WAHA poll settlement %s failed", phase)

    def _schedule_recovery(self, message_id: str, timestamp: float | None) -> None:
        if self._stopping:
            return
        current = self._recovery.get(message_id)
        attempts = current[1] if current and current[0] == timestamp else 0
        if (
            current is None and len(self._recovery) >= MAX_RECOVERY_POLLS
        ) or attempts >= len(RECOVERY_DELAYS):
            self._failures["retry_exhausted"] += 1
            return
        self._recovery[message_id] = (timestamp, attempts + 1)
        pending = self._registry.pending(message_id)
        deadline = max(
            time.time() + RECOVERY_DELAYS[attempts],
            pending.commit_at if pending and pending.commit_at else 0,
        )
        self._schedule_commit(message_id, timestamp, deadline, recovery=True)

    async def _async_recover(self, message_id: str, timestamp: float | None) -> None:
        async with self._lock:
            if self._stopping:
                return
            pending = self._registry.pending(message_id)
            if pending is None or pending.latest_vote_timestamp != timestamp:
                return
            if pending.selected_title is None:
                try:
                    cancelled = await self._write_registry(self._registry)
                except Exception:
                    self._failure("persistence")
                    self._schedule_recovery(message_id, timestamp)
                    return
                self._recovery.pop(message_id, None)
                if cancelled:
                    raise asyncio.CancelledError
                return
        await self._async_commit(message_id, timestamp)

    def _current_application_route(self, pending: PendingPoll) -> bool:
        return (
            pending.conversation_id is not None
            and self._channel_registry.resolve_conversation(pending.conversation_id)
            is not None
        )

    async def _async_commit(self, message_id: str, vote_timestamp: float) -> None:
        """Prepare, persist, then expose one settlement; never undo a newer vote."""
        async with self._lock:
            if self._stopping or self._writer_slot.owner is not self:
                return
            pending = self._registry.pending(message_id)
            if (
                pending is None
                or pending.latest_vote_timestamp != vote_timestamp
                or pending.selected_title is None
                or pending.commit_at is None
            ):
                return
            if pending.commit_at > time.time():
                self._schedule_commit(message_id, vote_timestamp, pending.commit_at)
                return
            action = pending.options[pending.selected_title]
            selection = None
            try:
                selection = self._settled_selection(pending, vote_timestamp, action)
                if pending.origin == "application" and selection is None:
                    raise ValueError("Application poll route unavailable")
            except Exception:
                self._failure("construction")
                if pending.origin == "application":
                    self._schedule_recovery(message_id, vote_timestamp)
                    return
                # Legacy channel observation is optional; action still settles.
            try:
                staged = deepcopy(self._registry)
                committed, _ = staged.commit(message_id, vote_timestamp, time.time())
                if not committed:
                    return
                if pending.origin == "application":
                    json.dumps(selection, allow_nan=False)
                    staged.remember_selection(selection)
            except Exception:
                self._failure("construction")
                self._schedule_recovery(message_id, vote_timestamp)
                return
            try:
                cancelled = await self._write_registry(staged)
            except Exception:
                self._failure("persistence")
                self._schedule_recovery(message_id, vote_timestamp)
                return
            # No staged application receipt is visible to selections() until
            # this exact snapshot has received physical persistence acknowledgement.
            self._registry = staged
            self._cancel_work(message_id)
            self._recovery.pop(message_id, None)
            if cancelled and pending.origin == "application":
                self._failure("publication")
                raise asyncio.CancelledError
            await self._async_publish(pending, vote_timestamp, action, selection)
            if cancelled:
                # A consumed legacy poll has no replay journal. Cancellation
                # after its acknowledged write must still make one action
                # publication attempt before propagating to the caller.
                raise asyncio.CancelledError

    async def _async_publish(
        self,
        pending: PendingPoll,
        vote_timestamp: float,
        action: str | None,
        selection: dict | None,
    ) -> None:
        cancelled = False
        try:
            context = await self._async_person_context(pending.person_entity_id)
        except asyncio.CancelledError:
            self._failure("publication")
            cancelled = True
            context = None
        except Exception:
            self._failure("publication")
            context = None
        if action is not None and pending.emit_legacy_action:
            try:
                self._hass.bus.async_fire(
                    EVENT_MOBILE_APP_NOTIFICATION_ACTION,
                    {"action": action},
                    context=context,
                )
            except Exception:
                self._failure("publication")
        if selection is not None and (
            pending.origin != "application" or self._current_application_route(pending)
        ):
            try:
                self._hass.bus.async_fire(
                    EVENT_WAHA_WHATSAPP, deepcopy(selection), context=context
                )
            except Exception:
                # A durable application receipt survives a missed bus event;
                # consumer replay recovers it without automatic re-publication.
                self._failure("publication")
        elif pending.origin == "application":
            self._failure("publication")
        if cancelled:
            raise asyncio.CancelledError

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

    async def _write_registry(self, registry: PollRegistry) -> bool:
        """Drain physical writes even if cancelled; report cancellation after ack.

        A cancelled DurableStore call can already have replaced the snapshot.
        Shield its entire lifetime so the caller can still promote exactly the
        acknowledged staged state instead of overwriting a durable receipt later.
        """
        work = asyncio.create_task(self._store.async_save(registry.as_dict()))
        cancelled = False
        while True:
            try:
                await asyncio.shield(work)
                return cancelled
            except asyncio.CancelledError:
                if work.cancelled():
                    raise
                cancelled = True

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
