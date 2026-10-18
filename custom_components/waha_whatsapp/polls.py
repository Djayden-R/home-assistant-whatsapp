"""Provider-independent actionable poll state and validation."""

from __future__ import annotations

import hashlib
import hmac
import math
import time
from collections.abc import Mapping, Sequence
from copy import deepcopy
from dataclasses import dataclass
from enum import StrEnum
from typing import Any


class PollValidationError(ValueError):
    """Raised when an actionable poll cannot be represented safely."""


class PollVoteRejectionReason(StrEnum):
    """Non-sensitive reason an authenticated vote was not accepted."""

    INVALID_EVENT_OR_SESSION = "invalid_event_or_session"
    MALFORMED_PAYLOAD = "malformed_payload"
    INVALID_DIRECTION = "invalid_direction"
    INVALID_FIELDS = "invalid_fields"
    MULTIPLE_SELECTIONS = "multiple_selections"
    UNKNOWN_POLL = "unknown_poll"
    LID_RESOLUTION_FAILED = "lid_resolution_failed"
    POLL_CHAT_MISMATCH = "poll_chat_mismatch"
    SENDER_IDENTITY_MISMATCH = "sender_identity_mismatch"
    STALE_VOTE = "stale_vote"
    UNKNOWN_OPTION = "unknown_option"


def verify_hmac_sha512(secret: str, raw_body: bytes, signature: str | None) -> bool:
    """Verify WAHA's SHA-512 webhook signature in constant time."""
    if not signature:
        return False
    expected = hmac.new(secret.encode(), raw_body, hashlib.sha512).hexdigest()
    return hmac.compare_digest(expected, signature.lower())


@dataclass(frozen=True, slots=True)
class PollOption:
    """One visible WhatsApp poll option and its Home Assistant action."""

    title: str
    action: str | None


@dataclass(frozen=True, slots=True)
class PollVote:
    """Validated WAHA vote data used to update a pending poll."""

    message_id: str
    sender: str
    poll_chat_id: str
    selected_title: str | None
    timestamp: float


@dataclass(slots=True)
class PendingPoll:
    """An outbound poll waiting for one stable vote."""

    message_id: str
    chat_id: str
    options: dict[str, str | None]
    settle_seconds: float
    person_entity_id: str | None = None
    conversation_id: str | None = None
    origin: str = "automation"
    client_request_id: str | None = None
    emit_legacy_action: bool = True
    latest_vote_timestamp: float | None = None
    selected_title: str | None = None
    commit_at: float | None = None

    def as_dict(self) -> dict[str, Any]:
        """Return JSON-serializable persistent state."""
        return {
            "message_id": self.message_id,
            "chat_id": self.chat_id,
            "options": self.options,
            "settle_seconds": self.settle_seconds,
            "person_entity_id": self.person_entity_id,
            "conversation_id": self.conversation_id,
            "origin": self.origin,
            "client_request_id": self.client_request_id,
            "emit_legacy_action": self.emit_legacy_action,
            "latest_vote_timestamp": self.latest_vote_timestamp,
            "selected_title": self.selected_title,
            "commit_at": self.commit_at,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> PendingPoll | None:
        """Restore one valid pending poll and reject malformed storage."""
        message_id = data.get("message_id")
        chat_id = data.get("chat_id")
        raw_options = data.get("options")
        settle_seconds = data.get("settle_seconds")
        person_entity_id = data.get("person_entity_id")
        conversation_id = data.get("conversation_id")
        origin = data.get("origin", "automation")
        request_id = data.get("client_request_id")
        legacy = data.get("emit_legacy_action", True)
        if (
            not isinstance(message_id, str)
            or not message_id
            or not isinstance(chat_id, str)
            or not chat_id
            or not isinstance(raw_options, dict)
            or isinstance(settle_seconds, bool)
            or not isinstance(settle_seconds, int | float)
            or not math.isfinite(settle_seconds)
            or not 0 <= settle_seconds <= 30
            or origin not in {"automation", "application"}
            or type(legacy) is not bool
            or (
                request_id is not None
                and (not isinstance(request_id, str) or not 1 <= len(request_id) <= 128)
            )
            or (origin == "application" and (not request_id or legacy))
            or (
                person_entity_id is not None
                and (
                    not isinstance(person_entity_id, str)
                    or not person_entity_id.startswith("person.")
                )
            )
            or (
                conversation_id is not None
                and (not isinstance(conversation_id, str) or not conversation_id)
            )
        ):
            return None

        options: dict[str, str | None] = {}
        for title, action in raw_options.items():
            if (
                not isinstance(title, str)
                or not title
                or (action is not None and (not isinstance(action, str) or not action))
            ):
                return None
            options[title] = action
        if len(options) < 2:
            return None

        latest_timestamp = data.get("latest_vote_timestamp")
        selected_title = data.get("selected_title")
        commit_at = data.get("commit_at")
        if latest_timestamp is not None and not isinstance(
            latest_timestamp, int | float
        ):
            return None
        if selected_title is not None and (
            not isinstance(selected_title, str) or selected_title not in options
        ):
            return None
        if commit_at is not None and not isinstance(commit_at, int | float):
            return None

        return cls(
            message_id=canonical_message_id(message_id),
            chat_id=chat_id,
            options=options,
            settle_seconds=float(settle_seconds),
            person_entity_id=person_entity_id,
            conversation_id=conversation_id,
            origin=origin,
            client_request_id=request_id,
            emit_legacy_action=legacy,
            latest_vote_timestamp=(
                float(latest_timestamp) if latest_timestamp is not None else None
            ),
            selected_title=selected_title,
            commit_at=float(commit_at) if commit_at is not None else None,
        )


def build_poll_options(
    actions: Sequence[Mapping[str, Any]], no_action_title: str
) -> tuple[PollOption, ...]:
    """Convert Companion App-style actions into unique poll options."""
    if not 1 <= len(actions) <= 12:
        raise PollValidationError("A poll requires between 1 and 12 actions")

    options: list[PollOption] = []
    seen_titles: set[str] = set()
    seen_actions: set[str] = set()
    for item in actions:
        action = item.get("action")
        title = item.get("title")
        if not isinstance(action, str) or not action.strip():
            raise PollValidationError("Every poll action requires an action ID")
        if not isinstance(title, str) or not title.strip():
            raise PollValidationError("Every poll action requires a title")

        normalized_action = action.strip()
        normalized_title = title.strip()
        if normalized_action in {"REPLY", "URI"} or item.get("behavior") == "textInput":
            raise PollValidationError(
                "Reply-input and URI actions cannot be represented by a poll"
            )
        if normalized_action in seen_actions:
            raise PollValidationError("Poll action IDs must be unique")
        if normalized_title in seen_titles:
            raise PollValidationError("Poll option titles must be unique")
        seen_actions.add(normalized_action)
        seen_titles.add(normalized_title)
        options.append(PollOption(normalized_title, normalized_action))

    if len(options) == 1:
        normalized_no_action = no_action_title.strip()
        if not normalized_no_action:
            raise PollValidationError("The no-action poll option requires a title")
        if normalized_no_action in seen_titles:
            raise PollValidationError("The no-action title must be unique")
        options.append(PollOption(normalized_no_action, None))

    return tuple(options)


def parse_poll_vote(data: Mapping[str, Any], session_name: str) -> PollVote | None:
    """Validate the relevant fields in a WAHA poll vote webhook."""
    vote, _reason = parse_poll_vote_with_reason(data, session_name)
    return vote


def parse_poll_vote_with_reason(
    data: Mapping[str, Any], session_name: str
) -> tuple[PollVote | None, PollVoteRejectionReason | None]:
    """Validate a vote and return a safe rejection reason when invalid."""
    if data.get("event") != "poll.vote" or data.get("session") != session_name:
        return None, PollVoteRejectionReason.INVALID_EVENT_OR_SESSION
    payload = data.get("payload")
    if not isinstance(payload, Mapping):
        return None, PollVoteRejectionReason.MALFORMED_PAYLOAD
    vote = payload.get("vote")
    poll = payload.get("poll")
    if not isinstance(vote, Mapping) or not isinstance(poll, Mapping):
        return None, PollVoteRejectionReason.MALFORMED_PAYLOAD
    if vote.get("fromMe") is not False or poll.get("fromMe") is not True:
        return None, PollVoteRejectionReason.INVALID_DIRECTION

    message_id = poll.get("id")
    sender = vote.get("from")
    poll_chat_id = poll.get("to")
    timestamp = vote.get("timestamp")
    selected_options = vote.get("selectedOptions")
    if (
        not isinstance(message_id, str)
        or not message_id
        or not isinstance(sender, str)
        or not sender
        or not isinstance(poll_chat_id, str)
        or not poll_chat_id
        or not isinstance(timestamp, int | float)
        or not isinstance(selected_options, list)
    ):
        return None, PollVoteRejectionReason.INVALID_FIELDS
    if len(selected_options) > 1:
        return None, PollVoteRejectionReason.MULTIPLE_SELECTIONS

    selected_title: str | None = None
    if selected_options:
        selected_title = selected_options[0]
        if not isinstance(selected_title, str) or not selected_title:
            return None, PollVoteRejectionReason.INVALID_FIELDS

    return (
        PollVote(
            message_id=message_id,
            sender=sender,
            poll_chat_id=poll_chat_id,
            selected_title=selected_title,
            timestamp=float(timestamp),
        ),
        None,
    )


class PollRegistry:
    """Track pending polls and settle only their newest valid vote."""

    def __init__(self) -> None:
        """Initialize an empty registry."""
        self._polls: dict[str, PendingPoll] = {}
        self._selections: list[dict[str, Any]] = []
        self._selection_sequence = 0

    def register(
        self,
        message_id: str,
        chat_id: str,
        options: Sequence[PollOption],
        settle_seconds: float,
        person_entity_id: str | None = None,
        conversation_id: str | None = None,
        *,
        origin: str = "automation",
        client_request_id: str | None = None,
        emit_legacy_action: bool = True,
    ) -> None:
        """Register one outbound poll by its WAHA message ID."""
        if (
            origin not in {"automation", "application"}
            or type(emit_legacy_action) is not bool
        ):
            raise PollValidationError("Invalid poll origin")
        if origin == "application" and (
            not isinstance(client_request_id, str)
            or not 1 <= len(client_request_id) <= 128
            or emit_legacy_action
            or not conversation_id
        ):
            raise PollValidationError("Application polls require durable correlation")
        message_id = canonical_message_id(message_id)
        self._polls[message_id] = PendingPoll(
            message_id=message_id,
            chat_id=chat_id,
            options={option.title: option.action for option in options},
            settle_seconds=settle_seconds,
            person_entity_id=person_entity_id,
            conversation_id=conversation_id,
            origin=origin,
            client_request_id=client_request_id,
            emit_legacy_action=emit_legacy_action,
        )

    def apply_vote(self, vote: PollVote, received_at: float) -> bool:
        """Apply a newer authenticated vote and start its settling window."""
        return self.apply_vote_with_reason(vote, received_at) is None

    def apply_vote_with_reason(
        self, vote: PollVote, received_at: float
    ) -> PollVoteRejectionReason | None:
        """Apply a vote or return a non-sensitive reason for rejecting it."""
        pending = self._polls.get(canonical_message_id(vote.message_id))
        if pending is None:
            return PollVoteRejectionReason.UNKNOWN_POLL
        if not _same_chat_id(vote.poll_chat_id, pending.chat_id):
            return PollVoteRejectionReason.POLL_CHAT_MISMATCH
        if not _same_chat_id(vote.sender, pending.chat_id):
            return PollVoteRejectionReason.SENDER_IDENTITY_MISMATCH
        if (
            pending.latest_vote_timestamp is not None
            and vote.timestamp <= pending.latest_vote_timestamp
        ):
            return PollVoteRejectionReason.STALE_VOTE
        if (
            vote.selected_title is not None
            and vote.selected_title not in pending.options
        ):
            return PollVoteRejectionReason.UNKNOWN_OPTION

        pending.latest_vote_timestamp = vote.timestamp
        pending.selected_title = vote.selected_title
        pending.commit_at = (
            received_at + pending.settle_seconds
            if vote.selected_title is not None
            else None
        )
        return None

    def pending(self, message_id: str) -> PendingPoll | None:
        """Return pending state for scheduling and diagnostics."""
        return self._polls.get(canonical_message_id(message_id))

    def pending_polls(self) -> tuple[PendingPoll, ...]:
        """Return a stable snapshot for restoring settle timers."""
        return tuple(self._polls.values())

    def commit(
        self, message_id: str, vote_timestamp: float, now: float
    ) -> tuple[bool, str | None]:
        """Consume a stable vote and return its optional action ID."""
        message_id = canonical_message_id(message_id)
        pending = self._polls.get(message_id)
        if (
            pending is None
            or pending.latest_vote_timestamp != vote_timestamp
            or pending.selected_title is None
            or pending.commit_at is None
            or pending.commit_at > now
        ):
            return False, None

        action = pending.options[pending.selected_title]
        del self._polls[message_id]
        return True, action

    def restore_uncommitted(self, pending: PendingPoll, selection: dict | None) -> None:
        """Retain an unannounced vote after a failed acknowledged write."""
        self._polls.setdefault(pending.message_id, pending)
        if selection is not None:
            self._selections = [
                r
                for r in self._selections
                if r["payload"].get("selection_sequence")
                != selection.get("selection_sequence")
            ]

    def as_dict(self) -> dict[str, Any]:
        """Serialize all unprocessed polls."""
        return {
            "polls": [pending.as_dict() for pending in self._polls.values()],
            "selections": deepcopy(self._selections),
            "selection_sequence": self._selection_sequence,
        }

    def remember_selection(self, payload: dict[str, Any]) -> None:
        """Retain bounded application receipts for reconnect recovery."""
        self._selection_sequence += 1
        payload["selection_sequence"] = self._selection_sequence
        cutoff = time.time() - 7 * 86400
        self._selections = [r for r in self._selections if r["saved_at"] >= cutoff][
            -2047:
        ]
        self._selections.append({"payload": deepcopy(payload), "saved_at": time.time()})

    def selections(
        self, conversation_id: str, after_sequence: int, limit: int
    ) -> dict[str, Any]:
        """Return phone-free immutable application events in ascending sequence."""
        if (
            type(after_sequence) is not int
            or after_sequence < 0
            or type(limit) is not int
            or not 1 <= limit <= 100
        ):
            raise PollValidationError("Invalid receipt page")
        cutoff = time.time() - 7 * 86400
        rows = [
            r["payload"]
            for r in self._selections
            if r["saved_at"] >= cutoff
            and r["payload"].get("conversation_id") == conversation_id
            and r["payload"].get("selection_sequence", 0) > after_sequence
        ]
        page = rows[:limit]
        return {
            "events": deepcopy(page),
            "has_more": len(rows) > limit,
            "next_sequence": page[-1]["selection_sequence"]
            if page
            else self._selection_sequence,
        }

    def load(self, data: Mapping[str, Any] | None) -> None:
        """Replace registry contents with valid stored records."""
        self._polls.clear()
        self._selections.clear()
        self._selection_sequence = 0
        if not isinstance(data, Mapping) or not isinstance(data.get("polls"), list):
            return
        for raw_pending in data["polls"]:
            if not isinstance(raw_pending, Mapping):
                continue
            pending = PendingPoll.from_dict(raw_pending)
            if pending is not None:
                self._polls[pending.message_id] = pending
        sequence = data.get("selection_sequence", 0)
        if type(sequence) is int and sequence >= 0:
            self._selection_sequence = sequence
        rows = data.get("selections", [])
        if not isinstance(rows, list):
            return
        seen = set()
        for row in rows[-2048:]:
            if not isinstance(row, dict):
                continue
            saved = row.get("saved_at")
            payload = row.get("payload")
            if (
                isinstance(saved, bool)
                or not isinstance(saved, int | float)
                or not math.isfinite(saved)
                or not time.time() - 7 * 86400 <= saved <= time.time() + 300
                or not isinstance(payload, dict)
                or payload.get("origin") != "application"
                or payload.get("type") != "poll.selection_settled"
                or type(payload.get("selection_sequence")) is not int
                or payload["selection_sequence"] < 1
                or payload["selection_sequence"] in seen
                or any(
                    not isinstance(payload.get(k), str)
                    or not 1 <= len(payload[k]) <= 256
                    for k in (
                        "conversation_id",
                        "config_entry_id",
                        "event_id",
                        "client_request_id",
                        "vote_occurred_at",
                    )
                )
                or not isinstance(payload.get("sender"), dict)
                or not isinstance(payload.get("poll"), dict)
                or any(
                    not isinstance(payload["poll"].get(k), str)
                    or not 1 <= len(payload["poll"][k]) <= 256
                    for k in ("message_id", "action_id")
                )
            ):
                continue
            seen.add(payload["selection_sequence"])
            self._selections.append(deepcopy(row))
            self._selection_sequence = max(
                self._selection_sequence, payload["selection_sequence"]
            )
        self._selections.sort(key=lambda r: r["payload"]["selection_sequence"])


def _same_chat_id(left: str, right: str) -> bool:
    """Compare WAHA chat IDs case-insensitively without exposing them."""
    return left.casefold() == right.casefold()


def canonical_message_id(message_id: str) -> str:
    """Extract the engine-stable token from a serialized WAHA message ID."""
    parts = message_id.split("_")
    if (
        len(parts) in (3, 4)
        and parts[0].casefold() in {"true", "false"}
        and "@" in parts[1]
        and parts[2]
    ):
        return parts[2]
    return message_id


def resolve_vote_lids(vote: PollVote, mappings: Mapping[str, str]) -> PollVote | None:
    """Replace LID identities using verified WAHA phone-number mappings."""

    def _resolve(value: str) -> str | None:
        if not is_lid_chat_id(value):
            return value
        return mappings.get(lid_mapping_id(value).casefold())

    sender = _resolve(vote.sender)
    poll_chat_id = _resolve(vote.poll_chat_id)
    if sender is None or poll_chat_id is None:
        return None
    return PollVote(
        message_id=vote.message_id,
        sender=sender,
        poll_chat_id=poll_chat_id,
        selected_title=vote.selected_title,
        timestamp=vote.timestamp,
    )


def is_lid_chat_id(value: str) -> bool:
    """Return whether a chat ID is a syntactically valid WhatsApp LID."""
    local, separator, domain = value.rpartition("@")
    if not separator or domain.casefold() != "lid":
        return False
    account, device_separator, device = local.partition(":")
    return account.isdecimal() and (
        not device_separator or (device.isdecimal() and ":" not in device)
    )


def lid_mapping_id(value: str) -> str:
    """Remove an optional device suffix before querying WAHA's LID API."""
    local, _separator, _domain = value.rpartition("@")
    account = local.partition(":")[0]
    return f"{account}@lid"
