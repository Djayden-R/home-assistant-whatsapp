"""Consumer-directed exact reads and bounded, ephemeral typing leases."""

from __future__ import annotations

import asyncio
import json
import re
import time
from collections.abc import Awaitable, Callable, Mapping
from contextlib import suppress
from dataclasses import dataclass
from typing import Any

from .api import WahaAuthenticationError, WahaRequestError
from .general_groups import participant_alias

MIN_ACTIVITY_GOWS_VERSION = (2026, 9, 1)
MAX_ACTIVITY_CONVERSATIONS = 256
MAX_ACTIVITY_IDS = 2048
_ACTIVITY_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}\Z", re.ASCII)
_CODES = frozenset(
    {
        "invalid_request",
        "not_authorized",
        "invalid_conversation",
        "invalid_message",
        "unsupported",
        "channel_unavailable",
        "rate_limited",
        "transport_failure",
        "outcome_unknown",
    }
)


class ActivityError(Exception):
    """Expose only bounded public activity outcomes."""

    def __init__(self, code: str, *, status: str = "failed") -> None:
        if code not in _CODES or status not in {"failed", "unknown"}:
            raise ValueError("Invalid activity outcome")
        self.code = code
        self.status = status
        super().__init__(code)


def validate_activity_request(data: Mapping[str, Any], *, typing: bool) -> None:
    """Validate without coercion, including booleans masquerading as integers."""
    required = {"config_entry_id", "conversation_id"}
    required |= {"activity_id", "state"} if typing else {"message_id"}
    allowed = required | ({"ttl_seconds"} if typing else set())
    if not required <= data.keys() or data.keys() - allowed:
        raise ActivityError("invalid_request")
    for key in (
        "config_entry_id",
        "conversation_id",
        *(() if typing else ("message_id",)),
    ):
        if not isinstance(data[key], str) or not 1 <= len(data[key]) <= 256:
            raise ActivityError("invalid_request")
    if not typing:
        return
    if (
        not isinstance(data["activity_id"], str)
        or _ACTIVITY_ID.fullmatch(data["activity_id"]) is None
        or data["state"] not in ("typing", "paused")
        or (data["state"] == "paused" and "ttl_seconds" in data)
    ):
        raise ActivityError("invalid_request")
    if "ttl_seconds" in data and (
        type(data["ttl_seconds"]) is not int or not 3 <= data["ttl_seconds"] <= 15
    ):
        raise ActivityError("invalid_request")


def activity_response(
    data: Mapping[str, Any],
    *,
    typing: bool,
    error: ActivityError | None = None,
    applied: bool = True,
) -> dict:
    """Echo the contract identities for every outcome, without provider detail."""
    identity = "activity_id" if typing else "message_id"
    result = {
        "schema_version": 1,
        "conversation_id": data.get("conversation_id"),
        identity: data.get(identity),
        "status": error.status if error else "accepted",
    }
    if typing:
        result["applied"] = (
            (None if error.status == "unknown" else False) if error else applied
        )
    if error:
        result["error_code"] = error.code
    return result


def _supported(server: Any) -> bool:
    version = re.fullmatch(
        r"(\d{4})\.(\d{1,2})\.(\d+)(?:\+[0-9A-Za-z.-]+)?", server.version or ""
    )
    return bool(
        (server.engine or "").upper() == "GOWS"
        and version
        and tuple(map(int, version.groups())) >= MIN_ACTIVITY_GOWS_VERSION
    )


def _route(
    entry: Any,
    conversation_id: str,
    participant: str | None = None,
    *,
    allow_unbound: bool = False,
) -> tuple:
    """Capture the exact currently configured account, route and membership."""
    runtime = entry.runtime_data
    manager = getattr(runtime, "activity_manager", None)
    account = (
        manager._account
        if manager is not None
        else participant_alias(runtime.session.account_id)
    )
    if account is None and not allow_unbound:
        raise ActivityError("channel_unavailable")
    if contact := runtime.channel_registry.resolve_conversation(conversation_id):
        route = ("direct", contact.subentry_id, contact.recipient)
        destination = f"{contact.recipient}@c.us"
    elif group := runtime.general_groups.resolve_conversation(conversation_id):
        route = ("general", group.subentry_id, group.group_jid, group.bot_account)
        destination = group.group_jid
    elif group_jid := runtime.guest_registry.resolve_group_conversation(
        conversation_id
    ):
        route = ("guest_group", group_jid)
        destination = group_jid
        if participant is not None:
            member = runtime.guest_registry.resolve_member(
                participant, occurred_at=time.time()
            )
            if member is None:
                raise ActivityError("invalid_conversation")
            route += (member["participant_id"], member["membership_id"])
    elif private := runtime.guest_registry.resolve_route(conversation_id):
        route = ("guest_direct", conversation_id, private)
        destination = private
    else:
        raise ActivityError("invalid_conversation")
    binding = json.dumps(
        [entry.data["session"], account, *route], separators=(",", ":")
    )
    return binding, destination, route[0]


def activity_observation_binding(
    entry: Any, conversation_id: str, *, raw_participant: str | None = None
) -> str | None:
    """Capture minimal incoming provenance; optional in legacy quote snapshots."""
    try:
        return _route(entry, conversation_id, raw_participant)[0]
    except ActivityError, AttributeError:
        return None


def incoming_activity_provenance(
    entry: Any,
    conversation_id: str,
    raw_id: str,
    raw_chat_id: str,
    *,
    raw_participant: str | None = None,
) -> dict[str, Any]:
    """Prove the serialized GOWS key cannot override its observed route.

    Quote IDs remain untouched. Reads use a separate normalized, incoming-only
    key assembled from the verified route and sender, never raw provider routing.
    Nonstandard/legacy IDs simply remain quote-only references.
    """
    if not isinstance(raw_id, str):
        return {}
    try:
        binding, destination, _ = _route(entry, conversation_id, raw_participant)
    except ActivityError, AttributeError:
        return {}
    parts = raw_id.split("_")
    if (
        len(parts) not in (3, 4)
        or parts[0] != "false"
        or re.fullmatch(r"[A-Za-z0-9.-]{1,256}", parts[2], re.ASCII) is None
    ):
        return {}
    remote = participant_alias(parts[1]) or parts[1]
    observed = participant_alias(raw_chat_id) or raw_chat_id
    if remote not in (observed, destination):
        return {}
    sender = participant_alias(raw_participant)
    if destination.endswith("@g.us"):
        if sender is None or (
            len(parts) == 4 and participant_alias(parts[3]) != sender
        ):
            return {}
    elif len(parts) == 4:
        return {}
    else:
        sender = None
    key = f"false_{destination}_{parts[2]}" + (f"_{sender}" if sender else "")
    return {
        "incoming_binding": binding,
        "incoming_participant": sender,
        "incoming_read_id": key,
    }


@dataclass(slots=True)
class _Lease:
    activity_id: str
    binding: str
    destination: str
    cancel: Callable[[], None] | None = None


class ActivityManager:
    """Serialize activity writes and guard every cleanup against its exact lease.

    History is never evicted within a runtime: on overload new identities are
    refused. This preserves stale-ID exclusion even after long consumer delays.
    Session-wide serialization also prevents one conversation's final offline
    from racing another conversation's typing start.
    """

    def __init__(
        self, hass: Any, entry: Any, *, schedule: Callable | None = None
    ) -> None:
        self._hass = hass
        self._entry = entry
        self._runtime = entry.runtime_data
        self._account = participant_alias(self._runtime.session.account_id)
        self._runtime_account = self._account
        self._session_name = entry.data["session"]
        self._lock = asyncio.Lock()
        self._current: dict[str, _Lease] = {}
        self._history: set[tuple[str, str]] = set()
        self._conversations: set[str] = set()
        self._stopping = False
        self._generation = 0
        self._schedule = schedule or self._ha_schedule

    def _ha_schedule(self, seconds: int, callback: Callable) -> Callable[[], None]:
        from homeassistant.helpers.event import async_call_later

        return async_call_later(self._hass, seconds, callback)

    def _require_current(self, *, cleanup: bool = False) -> None:
        entry = self._entry
        if (
            entry.runtime_data is not self._runtime
            or entry.data["session"] != self._session_name
            or participant_alias(self._runtime.session.account_id)
            != self._runtime_account
            or (not cleanup and self._stopping)
            or (not cleanup and getattr(entry.state, "value", entry.state) != "loaded")
        ):
            raise ActivityError("channel_unavailable")

    def disconnected(self) -> None:
        """Retire ephemeral leases immediately; a reconnect never replays them."""
        self._generation += 1
        for lease in self._current.values():
            if lease.cancel:
                lease.cancel()
        self._current.clear()

    async def async_handle_session_status(self, payload: Mapping[str, Any]) -> None:
        """Retire activity only for this authenticated session's disconnect."""
        status = payload.get("payload")
        if payload.get("session") != self._session_name or not isinstance(
            status, Mapping
        ):
            return
        async with self._lock:
            if status.get("status") == "WORKING":
                with suppress(ActivityError):
                    await self._backend()
            elif status.get("status") in {
                "STOPPED",
                "STARTING",
                "SCAN_QR_CODE",
                "FAILED",
            }:
                self.disconnected()

    async def _backend(self, *, cleanup: bool = False) -> None:
        self._require_current(cleanup=cleanup)
        runtime = self._runtime
        generation = self._generation
        try:
            server = await runtime.client.async_get_server()
            session = await runtime.client.async_get_session()
        except Exception as err:
            # A lost verification response is not evidence of disconnection.
            # Preserve owned timers so already accepted/uncertain typing expires.
            raise ActivityError("channel_unavailable") from err
        self._require_current(cleanup=cleanup)
        if not _supported(server):
            raise ActivityError("unsupported")
        if (
            generation != self._generation
            or session.status != "WORKING"
            or session.name != self._session_name
            or participant_alias(session.account_id) is None
            or (
                self._account is not None
                and participant_alias(session.account_id) != self._account
            )
        ):
            self.disconnected()
            raise ActivityError("channel_unavailable")
        # Setup may precede the first connection. Bind once after positive
        # verification; never follow a subsequent account swap.
        if self._account is None:
            self._account = participant_alias(session.account_id)

    async def capabilities(self) -> dict[str, Any]:
        try:
            async with self._lock:
                await self._backend()
        except ActivityError:
            ready = False
        else:
            ready = True
        return {
            "schema_version": 1,
            "mark_read": ready,
            "typing": ready,
            "read_scope": "message",
        }

    async def _authorize(
        self,
        conversation_id: str,
        principal: Callable | None,
        *,
        participant: str | None = None,
        cleanup: bool = False,
    ) -> tuple:
        self._require_current(cleanup=cleanup)
        # Reject unknown routes before any backend reads/effects.
        unbound = self._account is None
        before = _route(self._entry, conversation_id, participant, allow_unbound=True)
        generation = self._generation
        await self._backend(cleanup=cleanup)
        if unbound:
            binding = json.loads(before[0])
            binding[1] = self._account
            before = (json.dumps(binding, separators=(",", ":")), *before[1:])
        runtime = self._runtime
        kind = before[2]
        if kind == "general":
            group = runtime.general_groups.resolve_conversation(conversation_id)
            try:
                proof = await runtime.general_groups.async_verify(group)
            except Exception as err:
                raise ActivityError("invalid_conversation") from err
            if participant is not None and proof.identity(participant) is None:
                raise ActivityError("invalid_message")
        elif kind.startswith("guest"):
            try:
                state = await runtime.guest_manager.async_reconcile(force=True)
                verified = state.get("ready") and (
                    await runtime.guest_manager.async_verify_group_destination(
                        before[1]
                    )
                    if kind == "guest_group"
                    else await runtime.guest_manager.async_verify_guest_destination(
                        before[1]
                    )
                )
                if verified and participant is not None:
                    verified = (
                        await runtime.guest_manager.async_verify_guest_destination(
                            participant
                        )
                    )
            except Exception as err:
                raise ActivityError("invalid_conversation") from err
            if not verified:
                raise ActivityError("invalid_conversation")
        if principal is not None:
            await principal()
        # Roster and principal checks can yield; verify the connected account
        # again at the write boundary instead of using an earlier GET snapshot.
        await self._backend(cleanup=cleanup)
        if principal is not None:
            await principal()
        self._require_current(cleanup=cleanup)
        if generation != self._generation or before != _route(
            self._entry, conversation_id, participant
        ):
            raise ActivityError("invalid_conversation")
        return before

    @staticmethod
    async def _effect(operation: Awaitable) -> None:
        try:
            await operation
        except WahaAuthenticationError as err:
            raise ActivityError("channel_unavailable") from err
        except WahaRequestError as err:
            if err.status >= 500:
                raise ActivityError("outcome_unknown", status="unknown") from err
            raise ActivityError(
                "rate_limited" if err.status == 429 else "transport_failure"
            ) from err
        except Exception as err:
            # Once submitted, a missing/malformed response cannot prove refusal.
            raise ActivityError("outcome_unknown", status="unknown") from err

    async def async_mark_read(
        self, data: Mapping[str, Any], principal: Callable
    ) -> dict:
        try:
            validate_activity_request(data, typing=False)
            if data["config_entry_id"] != self._entry.entry_id:
                raise ActivityError("invalid_conversation")
            async with self._lock:
                await principal()
                self._require_current()
                conversation = data["conversation_id"]
                registry = self._runtime.channel_registry
                message = registry.resolve_incoming_message(
                    data["message_id"], conversation
                )
                if message is None:
                    raise ActivityError("invalid_message")
                binding, destination, _ = await self._authorize(
                    conversation, principal, participant=message[2]
                )
                # Retention/ownership may change during roster and auth awaits.
                if (
                    registry.resolve_incoming_message(data["message_id"], conversation)
                    != message
                    or message[1] != binding
                    or (destination.endswith("@g.us") and message[2] is None)
                ):
                    raise ActivityError("invalid_message")
                normalized = incoming_activity_provenance(
                    self._entry,
                    conversation,
                    message[0],
                    destination,
                    raw_participant=message[2],
                )
                if (
                    normalized.get("incoming_read_id") != message[0]
                    or normalized.get("incoming_binding") != binding
                ):
                    raise ActivityError("invalid_message")
                await self._effect(
                    self._runtime.client.async_mark_seen(
                        destination, message[0], participant=message[2]
                    )
                )
            return activity_response(data, typing=False)
        except ActivityError as err:
            return activity_response(data, typing=False, error=err)

    async def async_set_typing(
        self, data: Mapping[str, Any], principal: Callable
    ) -> dict:
        try:
            validate_activity_request(data, typing=True)
            if data["config_entry_id"] != self._entry.entry_id:
                raise ActivityError("invalid_conversation")
            async with self._lock:
                await principal()
                self._require_current()
                conversation, activity = data["conversation_id"], data["activity_id"]
                lease = self._current.get(conversation)
                # Stale/duplicate operations still authenticate and validate the
                # current route, but never clear or resurrect another lifetime.
                binding, destination, _ = await self._authorize(conversation, principal)
                lease = self._current.get(conversation)
                if data["state"] == "paused":
                    if lease is None or lease.activity_id != activity:
                        self._admit_identity(conversation, activity)
                        return activity_response(data, typing=True, applied=False)
                    if lease.binding != binding:
                        raise ActivityError("invalid_conversation")
                    await self._clear(conversation, lease, authorized=True)
                    return activity_response(data, typing=True)
                key = (conversation, activity)
                if key in self._history and (
                    lease is None or lease.activity_id != activity
                ):
                    return activity_response(data, typing=True, applied=False)
                self._admit_identity(conversation, activity)
                if lease is not None and lease.binding != binding:
                    raise ActivityError("invalid_conversation")
                if lease and lease.cancel:
                    lease.cancel()
                lease = _Lease(activity, binding, destination)
                self._history.add(key)
                self._conversations.add(conversation)
                self._current[conversation] = lease

                async def _expire(_now) -> None:
                    async with self._lock:
                        if self._current.get(conversation) is not lease:
                            return
                        with suppress(ActivityError):
                            await self._clear(conversation, lease)

                # Install before submission so an uncertain start still expires.
                lease.cancel = self._schedule(data.get("ttl_seconds", 10), _expire)
                await self._effect(
                    self._runtime.client.async_set_presence(destination, "typing")
                )
            return activity_response(data, typing=True)
        except ActivityError as err:
            return activity_response(data, typing=True, error=err)

    def _admit_identity(self, conversation: str, activity: str) -> None:
        key = (conversation, activity)
        if (
            key not in self._history
            and len(self._history) >= MAX_ACTIVITY_IDS
            or conversation not in self._conversations
            and len(self._conversations) >= MAX_ACTIVITY_CONVERSATIONS
        ):
            raise ActivityError("rate_limited")
        self._history.add(key)
        self._conversations.add(conversation)

    async def _clear(
        self,
        conversation: str,
        lease: _Lease,
        *,
        authorized: bool = False,
        cleanup: bool = False,
    ) -> None:
        if self._current.get(conversation) is not lease:
            return
        error = None
        cancelled = False
        try:
            if not authorized:
                binding, _, _ = await self._authorize(
                    conversation, None, cleanup=cleanup
                )
                if binding != lease.binding:
                    raise ActivityError("invalid_conversation")
            if self._current.get(conversation) is lease:
                await self._effect(
                    self._runtime.client.async_set_presence(lease.destination, "paused")
                )
        except ActivityError as err:
            error = err
        except asyncio.CancelledError:
            # The write may have reached WAHA. Keep the lease's existing expiry
            # until its bounded cleanup can run; cancellation proves no outcome.
            cancelled = True
            raise
        finally:
            if not cancelled and self._current.get(conversation) is lease:
                self._current.pop(conversation)
            if not cancelled and lease.cancel:
                lease.cancel()
        # Offline is session-wide: only after all owned conversation leases end.
        if not self._current:
            try:
                await self._backend(cleanup=cleanup)
                await self._effect(
                    self._runtime.client.async_set_presence(None, "offline")
                )
            except ActivityError as err:
                error = error or err
        if error:
            raise error

    async def async_stop(self, _event: Any = None) -> None:
        """Fence starts, drain pending writes, and best-effort clean owned presence."""
        self._stopping = True
        async with self._lock:
            for conversation, lease in list(self._current.items()):
                with suppress(ActivityError):
                    await self._clear(conversation, lease, cleanup=True)
