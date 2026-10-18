"""Bounded durable communication idempotency, independent of any consumer.

Only digests and safe outcomes are retained. Uncertain submissions are never
retried, including after restart; their records deliberately outlive retention
until a future, explicit reconciliation facility can settle them.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import math
import re
import time
from collections.abc import Awaitable, Callable, Mapping
from copy import deepcopy
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .api import (
    WahaAuthenticationError,
    WahaRequestError,
    WahaSessionNotFoundError,
)

if TYPE_CHECKING:
    from homeassistant.config_entries import ConfigEntry
    from homeassistant.core import HomeAssistant

STORAGE_VERSION = 1
IDEMPOTENCY_RETENTION_SECONDS = 7 * 24 * 60 * 60
MAX_IDEMPOTENCY_RECORDS = 2048
MAX_IDEMPOTENCY_STORAGE_BYTES = 1024 * 1024
MAX_CLIENT_REQUEST_ID_LENGTH = 128
MAX_MESSAGE_ID_BYTES = 256
_STATUSES = frozenset({"pending", "accepted", "failed", "unknown"})
_CODES = frozenset(
    {
        "invalid_conversation",
        "invalid_reply_target",
        "request_conflict",
        "channel_unavailable",
        "persistence_unavailable",
        "rate_limited",
        "transport_failure",
        "outcome_unknown",
        "invalid_request",
        "not_authorized",
        "media_not_found",
        "media_expired",
        "media_disabled",
        "unsupported",
        "unsupported_media_type",
        "media_too_large",
        "invalid_media",
        "busy",
        "storage_unavailable",
    }
)
_DIGEST = re.compile(r"[0-9a-f]{64}\Z")


class CorrelatedSendError(Exception):
    """A safe preflight or transport outcome, containing no provider detail."""

    def __init__(self, code: str, *, status: str = "failed") -> None:
        """Accept only documented machine-readable failure codes."""
        if code not in _CODES or status not in {"failed", "unknown"}:
            raise ValueError("Invalid correlated send outcome")
        super().__init__(code)
        self.code = code
        self.status = status


def _encoded(value: object) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


def _safe_message_id(value: object) -> bool:
    return value is None or (
        isinstance(value, str)
        and bool(value)
        and len(value.encode("utf-8")) <= MAX_MESSAGE_ID_BYTES
    )


class DurableStore:
    """Use Store's private format with acknowledged fsync-backed writes.

    Store.async_save intentionally logs some write failures and defers writes
    while HA is stopping. Neither behavior can acknowledge a transport intent.
    Keep its public path/format, but call HA's atomic file helper directly and
    strictly load without Store's corrupt-file reset or in-memory cache.
    """

    def __init__(
        self,
        hass: HomeAssistant,
        version: int,
        key: str,
        *,
        max_storage_bytes: int = MAX_IDEMPOTENCY_STORAGE_BYTES,
    ) -> None:
        """Create a private, strictly loaded Store-compatible snapshot file."""
        from homeassistant.helpers.storage import Store

        self._hass = hass
        self._max_storage_bytes = max_storage_bytes
        self._store = Store[dict[str, Any]](
            hass, version, key, private=True, atomic_writes=True
        )

    async def async_load(self) -> dict[str, Any] | None:
        """Read a real snapshot without corruption reset or cached state."""
        return await self._async_wait_io(self._hass.async_add_executor_job(self._load))

    def _load(self) -> dict[str, Any] | None:
        try:
            with Path(self._store.path).open("rb") as stream:
                raw = stream.read(self._max_storage_bytes + 1)
        except FileNotFoundError:
            return None
        if len(raw) > self._max_storage_bytes:
            raise ValueError("Idempotency envelope exceeds limits")
        envelope = json.loads(raw)
        if (
            not isinstance(envelope, dict)
            or envelope.get("version") != self._store.version
            or envelope.get("minor_version") != self._store.minor_version
            or envelope.get("key") != self._store.key
            or not isinstance(envelope.get("data"), dict)
        ):
            raise ValueError("Invalid idempotency storage envelope")
        return envelope["data"]

    async def async_save(self, data: dict[str, Any]) -> None:
        """Acknowledge only completed atomic writes; propagate every failure."""
        envelope = {
            "version": self._store.version,
            "minor_version": self._store.minor_version,
            "key": self._store.key,
            "data": deepcopy(data),
        }
        await self._async_wait_io(
            self._hass.async_add_executor_job(self._write, envelope)
        )

    @staticmethod
    async def _async_wait_io(work: asyncio.Future[Any]) -> Any:
        # Cancellation cannot release the writer lease while its executor is
        # still replacing the file. Finish the physical write, then propagate.
        cancelled = False
        while True:
            try:
                result = await asyncio.shield(work)
                break
            except asyncio.CancelledError:
                if work.cancelled():
                    raise
                cancelled = True
        if cancelled:
            raise asyncio.CancelledError
        return result

    def _write(self, envelope: dict[str, Any]) -> None:
        from homeassistant.util.file import write_utf8_file_atomic

        serialized = self._serialize(envelope)
        if len(serialized) > self._max_storage_bytes:
            raise ValueError("Private storage envelope exceeds limits")
        path = Path(self._store.path)
        path.parent.mkdir(parents=True, exist_ok=True)
        write_utf8_file_atomic(str(path), serialized, private=True, mode="wb")

    @staticmethod
    def _serialize(envelope: dict[str, Any]) -> bytes:
        return _encoded(envelope)


class _WriterSlot:
    """One entry's lifetime writer handoff within a Home Assistant process."""

    def __init__(self) -> None:
        self.lock = asyncio.Lock()
        self.owner: CorrelatedSendManager | None = None


class CorrelatedSendManager:
    """Persist intent before releasing the lock to perform a keyed send.

    Callers own route/quote validation in ``async_send`` and return an opaque
    message ID (or an outcome mapping). No raw provider IDs should be returned
    by that callback. A successful callback with no ID still means accepted.
    """

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        *,
        store: Any | None = None,
    ) -> None:
        """Use an entry-scoped atomic private Home Assistant Store."""
        self._entry_id = entry.entry_id
        self._store = (
            store
            if store is not None
            else DurableStore(
                hass,
                STORAGE_VERSION,
                f"waha_whatsapp.correlated.{entry.entry_id}",
                max_storage_bytes=MAX_IDEMPOTENCY_STORAGE_BYTES,
            )
        )
        if not hasattr(hass, "data"):
            hass.data = {}
        slots = hass.data.setdefault("waha_whatsapp.correlated_writers", {})
        self._writer_slot = slots.setdefault(entry.entry_id, _WriterSlot())
        self._records: dict[str, dict[str, Any]] = {}
        self._lock = asyncio.Lock()
        self.persistence_available = False
        self._started = False
        self._capacity_exhausted = False
        self._stopping = False
        self._active: set[asyncio.Future[None]] = set()
        self._drained = asyncio.Event()
        self._drained.set()

    @property
    def capabilities(self) -> dict[str, Any]:
        """Expose effective bounds and availability without private identities."""
        return {
            "correlated_sends": self.persistence_available,
            "idempotency_health": self.diagnostics["health"],
            "idempotency_retention_seconds": IDEMPOTENCY_RETENTION_SECONDS,
            "idempotency_max_records": MAX_IDEMPOTENCY_RECORDS,
            "idempotency_max_storage_bytes": MAX_IDEMPOTENCY_STORAGE_BYTES,
            "client_request_id_max_length": MAX_CLIENT_REQUEST_ID_LENGTH,
        }

    @property
    def diagnostics(self) -> dict[str, Any]:
        """Return sanitized aggregate state; no content, keys, or route IDs."""
        unknown = sum(r["status"] == "unknown" for r in self._records.values())
        pending = sum(r["status"] == "pending" for r in self._records.values())
        health = "ready"
        if not self.persistence_available:
            health = "unavailable"
        elif self._capacity_exhausted or unknown:
            health = "degraded"
        return {
            "health": health,
            "records": len(self._records),
            "pending": pending,
            "unknown": unknown,
            "storage_bytes": self._storage_size(self._snapshot()),
            "capacity_exhausted": self._capacity_exhausted,
        }

    def owns_request(self, conversation_id: str, client_request_id: str) -> bool:
        """Locate retained outcomes without authorizing a new send to a route.

        This read-only lookup lets entry selection find a saved outcome after a
        destination is removed. Eligibility checks still run in the callback
        for every new request; unresolved identities never expire implicitly.
        """
        if (
            not isinstance(conversation_id, str)
            or not conversation_id
            or not isinstance(client_request_id, str)
            or not client_request_id
            or len(client_request_id) > MAX_CLIENT_REQUEST_ID_LENGTH
        ):
            return False
        record = self._records.get(
            self._request_token(conversation_id, client_request_id)
        )
        return record is not None and (
            record["status"] in {"pending", "unknown"}
            or record["created_at"] + IDEMPOTENCY_RETENTION_SECONDS > time.time()
        )

    def _request_token(self, conversation_id: str, client_request_id: str) -> str:
        return hashlib.sha256(
            _encoded([self._entry_id, conversation_id, client_request_id])
        ).hexdigest()

    def media_binding(
        self,
        conversation_id: str,
        client_request_id: str,
        upload_token: str,
        principal_token: str,
    ) -> dict[str, Any] | None:
        """Resolve retained content facts only for a previously bound upload.

        Tokens are one-way hashes, never upload IDs or HA user identifiers.
        This permits a receipt replay after byte expiry without trusting an
        unknown upload ID. Route authorization remains the caller's duty.
        """
        self._prune(time.time())
        record = self._records.get(
            self._request_token(conversation_id, client_request_id)
        )
        media = record.get("media") if record else None
        if (
            media is None
            or media["principal_token"] != principal_token
            or upload_token not in media["upload_tokens"]
        ):
            return None
        return deepcopy(media)

    async def async_start(self) -> None:
        """Drain the previous runtime before loading this entry's ledger."""
        async with self._writer_slot.lock:
            if self._started or self._stopping:
                return
            previous = self._writer_slot.owner
            if previous is not None and previous is not self:
                await previous.async_stop()
            if self._stopping:
                return
            self._writer_slot.owner = self
            async with self._lock:
                self._started = True
                try:
                    data = await self._store.async_load()
                    if data is not None:
                        self._restore(data)
                    changed = self._prune(time.time())
                    for record in self._records.values():
                        if record["status"] == "pending":
                            record.update(
                                status="unknown", error_code="outcome_unknown"
                            )
                            changed = True
                    if changed:
                        await self._store.async_save(self._snapshot())
                except Exception:
                    # An unreadable record may fence an already accepted send.
                    self.persistence_available = False
                    return
                self.persistence_available = True

    async def async_stop(self) -> None:
        """Fence admission, then drain every admitted send and its outcome save.

        Cancelling the wait keeps the admission fence in place. Any replacement
        manager also waits on this drain before reading or writing the ledger.
        """
        async with self._lock:
            self._stopping = True
            self.persistence_available = False
        await self._drained.wait()

    async def async_execute(
        self,
        conversation_id: str,
        client_request_id: str,
        canonical_request: Mapping[str, Any],
        async_send: Callable[[], Awaitable[Any]],
        *,
        media_binding: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Replay the saved state, or durably claim and execute one send.

        ``canonical_request`` contains message, opaque reply_to_message_id and
        origin; absent quote/origin normalize to null/unknown. Other fields
        participate in the digest, so extensions cannot silently reuse a key.
        """
        if not isinstance(conversation_id, str) or not conversation_id:
            raise ValueError("Invalid conversation ID")
        if (
            not isinstance(client_request_id, str)
            or not client_request_id
            or len(client_request_id) > MAX_CLIENT_REQUEST_ID_LENGTH
        ):
            raise ValueError("Invalid client request ID")
        if not isinstance(canonical_request, Mapping):
            raise ValueError("Invalid canonical request")
        request = dict(canonical_request)
        request.setdefault("reply_to_message_id", None)
        request.setdefault("origin", "unknown")
        digest = hashlib.sha256(_encoded(request)).hexdigest()
        if media_binding is not None and not _valid_media_binding(media_binding):
            raise ValueError("Invalid media receipt binding")
        token = self._request_token(conversation_id, client_request_id)
        base = {
            "conversation_id": conversation_id,
            "client_request_id": client_request_id,
        }
        async with self._lock:
            self._prune(time.time())
            if existing := self._records.get(token):
                if existing["digest"] != digest:
                    return self._response(
                        base,
                        {"status": "failed", "error_code": "request_conflict"},
                        replayed=False,
                    )
                if media_binding is not None and "media" in existing:
                    # Remember a bounded set of identical-content restagings.
                    # Alias persistence precedes returning its replay receipt.
                    token_alias = media_binding["upload_tokens"][0]
                    aliases = existing["media"]["upload_tokens"]
                    if token_alias not in aliases:
                        # A new alias is a ledger write, not a read-only replay.
                        # A preflight may outlive stop and a writer handoff; its
                        # stale snapshot must never replace the new owner's data.
                        if self._stopping or self._writer_slot.owner is not self:
                            return self._response(
                                base,
                                {
                                    "status": "failed",
                                    "error_code": "channel_unavailable",
                                },
                                replayed=False,
                            )
                        if not self.persistence_available:
                            return self._response(
                                base,
                                {
                                    "status": "failed",
                                    "error_code": "persistence_unavailable",
                                },
                                replayed=False,
                            )
                        if len(aliases) >= 8:
                            return self._response(
                                base,
                                {"status": "failed", "error_code": "busy"},
                                replayed=False,
                            )
                        aliases.append(token_alias)
                        if not self._fits():
                            aliases.remove(token_alias)
                            return self._response(
                                base,
                                {
                                    "status": "failed",
                                    "error_code": "persistence_unavailable",
                                },
                                replayed=False,
                            )
                        try:
                            await self._store.async_save(self._snapshot())
                        except Exception:
                            aliases.remove(token_alias)
                            self.persistence_available = False
                            return self._response(
                                base,
                                {
                                    "status": "failed",
                                    "error_code": "persistence_unavailable",
                                },
                                replayed=False,
                            )
                return self._response(base, existing, replayed=True)
            if self._stopping:
                return self._response(
                    base,
                    {"status": "failed", "error_code": "channel_unavailable"},
                    replayed=False,
                )
            if not self.persistence_available:
                return self._response(
                    base,
                    {"status": "failed", "error_code": "persistence_unavailable"},
                    replayed=False,
                )
            record = {
                "digest": digest,
                "created_at": time.time(),
                "status": "pending",
                "message_id": None,
                "error_code": None,
            }
            if media_binding is not None:
                record["media"] = deepcopy(dict(media_binding))
            self._records[token] = record
            if not self._fits():
                del self._records[token]
                self._capacity_exhausted = True
                return self._response(
                    base,
                    {"status": "failed", "error_code": "persistence_unavailable"},
                    replayed=False,
                )
            try:
                await self._store.async_save(self._snapshot())
            except asyncio.CancelledError:
                record.update(status="unknown", error_code="outcome_unknown")
                self.persistence_available = False
                raise
            except Exception:
                record.update(status="failed", error_code="persistence_unavailable")
                self.persistence_available = False
                return self._response(base, record, replayed=False)
            self._capacity_exhausted = False
            active = asyncio.get_running_loop().create_future()
            self._active.add(active)
            self._drained.clear()
        try:
            return await self._async_execute_admitted(token, base, async_send)
        finally:
            self._active.discard(active)
            active.set_result(None)
            if not self._active:
                self._drained.set()

    async def _async_execute_admitted(
        self,
        token: str,
        base: dict[str, Any],
        async_send: Callable[[], Awaitable[Any]],
    ) -> dict[str, Any]:
        try:
            outcome = self._outcome(await async_send())
        except asyncio.CancelledError:
            await self._finish(
                token, {"status": "unknown", "error_code": "outcome_unknown"}
            )
            raise
        except CorrelatedSendError as err:
            outcome = {"status": err.status, "error_code": err.code}
        except WahaAuthenticationError, WahaSessionNotFoundError:
            outcome = {"status": "failed", "error_code": "channel_unavailable"}
        except WahaRequestError as err:
            if err.status == 429:
                outcome = {"status": "failed", "error_code": "rate_limited"}
            elif 400 <= err.status < 500:
                outcome = {"status": "failed", "error_code": "transport_failure"}
            else:
                outcome = {"status": "unknown", "error_code": "outcome_unknown"}
        except Exception:
            # Timeout, connection loss, malformed response, and unexpected
            # post-send failures cannot establish that WAHA rejected a send.
            outcome = {"status": "unknown", "error_code": "outcome_unknown"}
        record = await self._finish(token, outcome)
        return self._response(base, record, replayed=False)

    async def _finish(self, token: str, outcome: dict[str, Any]) -> dict[str, Any]:
        async with self._lock:
            record = self._records[token]
            record.update(message_id=None, error_code=None)
            record.update(outcome)
            try:
                await self._store.async_save(self._snapshot())
            except asyncio.CancelledError:
                record.update(
                    status="unknown", message_id=None, error_code="outcome_unknown"
                )
                self.persistence_available = False
                raise
            except Exception:
                # Retain the in-process fence as well as the durable pending
                # intent. Never re-submit merely because recording failed.
                record.update(
                    status="unknown", message_id=None, error_code="outcome_unknown"
                )
                self.persistence_available = False
            return deepcopy(record)

    @staticmethod
    def _outcome(value: Any) -> dict[str, Any]:
        if value is None or isinstance(value, str):
            result = {"status": "accepted", "message_id": value}
        elif isinstance(value, Mapping):
            result = {
                "status": value.get("status", "accepted"),
                "message_id": value.get("message_id"),
            }
            if value.get("error_code") is not None:
                result["error_code"] = value["error_code"]
        else:
            raise ValueError("Send callback must return an opaque result")
        if (
            result["status"] not in {"accepted", "failed", "unknown"}
            or not _safe_message_id(result.get("message_id"))
            or result.get("error_code") not in _CODES | {None}
        ):
            raise ValueError("Invalid send callback result")
        if result["status"] == "unknown":
            result["error_code"] = "outcome_unknown"
        elif result["status"] == "failed":
            result.setdefault("error_code", "transport_failure")
        else:
            result.pop("error_code", None)
        return result

    @staticmethod
    def _response(
        base: dict[str, Any], record: dict[str, Any], *, replayed: bool
    ) -> dict[str, Any]:
        response = {
            **base,
            "status": record["status"],
            "message_id": record.get("message_id"),
            "replayed": replayed,
        }
        if record.get("error_code") is not None:
            response["error_code"] = record["error_code"]
        return response

    def _prune(self, now: float) -> bool:
        before = len(self._records)
        self._records = {
            token: record
            for token, record in self._records.items()
            if record["status"] in {"pending", "unknown"}
            or record["created_at"] + IDEMPOTENCY_RETENTION_SECONDS > now
        }
        return len(self._records) != before

    def _snapshot(self) -> dict[str, Any]:
        return {"records": self._records}

    def _storage_size(self, data: object) -> int:
        return len(
            _encoded(
                {
                    "version": STORAGE_VERSION,
                    "minor_version": 1,
                    "key": f"waha_whatsapp.correlated.{self._entry_id}",
                    "data": data,
                }
            )
        )

    def _fits(self) -> bool:
        # Reserve worst-case escaped result fields before network I/O. Results
        # cannot push an already accepted intent beyond the configured limit.
        reserved = deepcopy(self._records)
        for record in reserved.values():
            if record["status"] in {"pending", "unknown"}:
                record.update(
                    message_id="\x00" * MAX_MESSAGE_ID_BYTES,
                    error_code="persistence_unavailable",
                )
        return (
            len(reserved) <= MAX_IDEMPOTENCY_RECORDS
            and self._storage_size({"records": reserved})
            <= MAX_IDEMPOTENCY_STORAGE_BYTES
        )

    def _restore(self, data: object) -> None:
        if not isinstance(data, Mapping) or not isinstance(data.get("records"), dict):
            raise ValueError("Invalid idempotency store")
        records = data["records"]
        if (
            len(records) > MAX_IDEMPOTENCY_RECORDS
            or self._storage_size(data) > MAX_IDEMPOTENCY_STORAGE_BYTES
        ):
            raise ValueError("Idempotency store exceeds limits")
        for token, record in records.items():
            if (
                not isinstance(token, str)
                or not _DIGEST.fullmatch(token)
                or not isinstance(record, dict)
                or set(record)
                not in (
                    {"digest", "created_at", "status", "message_id", "error_code"},
                    {
                        "digest",
                        "created_at",
                        "status",
                        "message_id",
                        "error_code",
                        "media",
                    },
                )
                or ("media" in record and not _valid_media_binding(record["media"]))
                or not isinstance(record["digest"], str)
                or not _DIGEST.fullmatch(record["digest"])
                or not isinstance(record["created_at"], (int, float))
                or isinstance(record["created_at"], bool)
                or not math.isfinite(record["created_at"])
                or record["status"] not in _STATUSES
                or not _safe_message_id(record["message_id"])
                or record["error_code"] not in _CODES | {None}
            ):
                raise ValueError("Invalid idempotency record")
        self._records = deepcopy(records)


def _valid_media_binding(value: object) -> bool:
    """Validate bounded, content-free extension while reading old v1 ledgers."""
    if not isinstance(value, Mapping) or set(value) != {
        "upload_tokens",
        "principal_token",
        "sha256",
        "size_bytes",
        "kind",
        "mime_type",
        "native_opus",
        "file_name_digest",
        "route_binding_digest",
    }:
        return False
    tokens = value["upload_tokens"]
    return (
        isinstance(tokens, list)
        and 1 <= len(tokens) <= 8
        and all(isinstance(item, str) and _DIGEST.fullmatch(item) for item in tokens)
        and len(set(tokens)) == len(tokens)
        and all(
            isinstance(value[field], str) and _DIGEST.fullmatch(value[field])
            for field in (
                "principal_token",
                "sha256",
                "file_name_digest",
                "route_binding_digest",
            )
        )
        and type(value["size_bytes"]) is int
        and 0 < value["size_bytes"] <= 25 * 1024 * 1024
        and value["kind"] in {"audio", "image", "file"}
        and isinstance(value["mime_type"], str)
        and 1 <= len(value["mime_type"]) <= 128
        and type(value["native_opus"]) is bool
    )
