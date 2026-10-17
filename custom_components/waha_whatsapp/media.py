"""Private bounded media references, cache, and outbound staging.

Public metadata never contains a backend URL or a local path. The shared store
owns all entries' budgets; route callbacks must return an immutable exact route
binding after fresh authorization (including current guest membership).
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import math
import os
import re
import secrets
import time
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

MAX_BYTES = 25 * 1024 * 1024
CACHE_MAX_BYTES = 256 * 1024 * 1024
CACHE_MAX_FILES = 128
REFERENCE_MAX_ITEMS = 2048
RETENTION_SECONDS = 3600
CHUNK_BYTES = 64 * 1024
QUEUE_MAX = 4
STORE_KEY = "waha_whatsapp_media_store"
MIME_TYPES = (
    "audio/ogg",
    "audio/mpeg",
    "audio/mp4",
    "audio/wav",
    "audio/flac",
    "image/jpeg",
    "image/png",
    "image/gif",
    "image/webp",
    "application/pdf",
)
_FILE = re.compile(r"^[a-f0-9]{32}\.bin$")


class MediaError(Exception):
    """A safe public error with no provider content."""

    def __init__(
        self, code: str, *, retryable: bool = False, http_status: int | None = None
    ):
        super().__init__(code)
        self.code = code
        self.retryable = retryable
        self.http_status = http_status or {
            "not_authorized": 403,
            "invalid_conversation": 404,
            "media_not_found": 404,
            "media_expired": 410,
            "media_too_large": 413,
            "unsupported_media_type": 415,
            "unsupported": 422,
            "request_conflict": 409,
            "busy": 429,
            "rate_limited": 429,
            "invalid_media": 422,
            "media_disabled": 503,
            "storage_unavailable": 503,
            "persistence_unavailable": 503,
            "channel_unavailable": 503,
            "transport_failure": 502,
            "outcome_unknown": 502,
        }.get(code, 400)

    def as_dict(self) -> dict[str, Any]:
        return {
            "status": "failed",
            "error": {"code": self.code, "retryable": self.retryable},
        }


def display_name(value: object) -> str | None:
    """Keep a bounded display label, never a path or header control sequence."""
    if not isinstance(value, str):
        return None
    value = value.replace("\\", "/").rsplit("/", 1)[-1]
    value = "".join(c for c in value if c.isprintable() and c not in "\r\n\x00")
    return value[:160] or None


def normalized_mime(value: object) -> str | None:
    if not isinstance(value, str) or len(value) > 128:
        return None
    value = value.split(";", 1)[0].strip().lower()
    return (
        value if re.fullmatch(r"[a-z0-9!#$&^_.+-]+/[a-z0-9!#$&^_.+-]+", value) else None
    )


def _number(value: object) -> int | None:
    return value if type(value) is int and 0 <= value <= 2**53 else None


def media_metadata(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Read GOWS protobuf transport facts; Ogg alone never establishes PTT."""
    media = payload.get("media")
    media = media if isinstance(media, Mapping) else {}
    raw = payload.get("_data")
    message = raw.get("Message") if isinstance(raw, Mapping) else None
    message = message if isinstance(message, Mapping) else {}
    audio = message.get("audioMessage")
    image = message.get("imageMessage")
    document = message.get("documentMessage")
    content = next(
        (item for item in (audio, image, document) if isinstance(item, Mapping)), {}
    )
    mime = normalized_mime(media.get("mimetype")) or normalized_mime(
        content.get("mimetype")
    )
    info = raw.get("Info") if isinstance(raw, Mapping) else None
    transport_kind = info.get("MediaType") if isinstance(info, Mapping) else None
    # GOWS preserves whatsmeow Info.MediaType; its native PTT classifier
    # distinguishes ptt/audio independently of MIME/container/filename.
    voice = (
        True
        if transport_kind == "ptt"
        else False
        if transport_kind == "audio"
        else None
    )
    if transport_kind in ("ptt", "audio") or isinstance(audio, Mapping):
        kind = "audio"
    elif transport_kind == "image" or isinstance(image, Mapping):
        kind = "image"
    elif transport_kind == "document" or isinstance(document, Mapping):
        kind = "file"
    elif mime and mime.startswith("audio/"):
        kind = "audio"
    elif mime and mime.startswith("image/"):
        kind = "image"
    else:
        kind = "file" if mime else "unknown"
    seconds = _number(content.get("seconds"))
    return {
        "kind": kind,
        "mime_type": mime,
        "file_name": display_name(media.get("filename", content.get("fileName"))),
        "size_bytes": _number(content.get("fileLength")),
        "voice_note": voice,
        "duration_ms": seconds * 1000 if seconds is not None else None,
    }


def sniff_media(head: bytes) -> tuple[str, str, bool]:
    """Establish supported containers from bytes; declarations are not proof."""
    if head.startswith(b"OggS"):
        if len(head) < 28 or head[4] != 0 or not head[5] & 2:
            raise MediaError("invalid_media")
        count = head[26]
        if not count or len(head) < 27 + count:
            raise MediaError("invalid_media")
        segments = head[27 : 27 + count]
        packet_length = 0
        for segment in segments:
            packet_length += segment
            if segment < 255:
                break
        else:
            raise MediaError("invalid_media")
        packet = head[27 + count : 27 + count + packet_length]
        if len(packet) != packet_length:
            raise MediaError("invalid_media")
        opus = (
            len(packet) >= 19
            and packet[:8] == b"OpusHead"
            and packet[8] == 1
            and packet[9] in (1, 2)
            and packet[18] == 0
            and len(packet) == 19
        )
        if packet.startswith(b"OpusHead") and not opus:
            raise MediaError("invalid_media")
        if not opus and not packet.startswith(b"\x01vorbis"):
            raise MediaError("unsupported_media_type", http_status=415)
        return "audio", "audio/ogg", opus
    if head.startswith(b"\xff\xd8\xff"):
        return "image", "image/jpeg", False
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image", "image/png", False
    if head.startswith((b"GIF87a", b"GIF89a")):
        return "image", "image/gif", False
    if head.startswith(b"RIFF") and head[8:12] == b"WEBP":
        return "image", "image/webp", False
    if head.startswith(b"RIFF") and head[8:12] == b"WAVE":
        return "audio", "audio/wav", False
    if head.startswith(b"fLaC"):
        return "audio", "audio/flac", False
    if (
        head.startswith(b"ID3")
        or len(head) >= 2
        and head[0] == 255
        and head[1] & 0xE0 == 0xE0
    ):
        return "audio", "audio/mpeg", False
    if head.startswith(b"%PDF-"):
        return "file", "application/pdf", False
    if len(head) >= 12 and head[4:8] == b"ftyp" and head[8:12] in (b"M4A ", b"M4B "):
        return "audio", "audio/mp4", False
    raise MediaError("unsupported_media_type", http_status=415)


def _ogg_crc_table() -> tuple[int, ...]:
    result = []
    for octet in range(256):
        value = octet << 24
        for _ in range(8):
            value = (
                (value << 1) ^ (0x04C11DB7 if value & 0x80000000 else 0)
            ) & 0xFFFFFFFF
        result.append(value)
    return tuple(result)


_OGG_CRC = _ogg_crc_table()


def _ogg_crc(data: bytes) -> int:
    value = 0
    for octet in data:
        value = ((value << 8) & 0xFFFFFFFF) ^ _OGG_CRC[((value >> 24) & 255) ^ octet]
    return value


def _validate_ogg(path: Path, native_opus: bool) -> None:
    """Validate bounded Ogg page framing and checksums, without decoding audio.

    Native voice v1 supports one mono/stereo Opus stream (mapping family zero).
    Packet data remains untrusted: this is transport validation, not sanitizing.
    """
    sequence, serial, packets = 0, None, 0
    partial = bytearray()
    ended = False
    with os.fdopen(os.open(path, os.O_RDONLY | os.O_NOFOLLOW), "rb") as stream:
        while header := stream.read(27):
            if (
                len(header) != 27
                or header[:5] != b"OggS\x00"
                or ended
                or header[5] & ~7
                or bool(header[5] & 2) != (sequence == 0)
                or bool(header[5] & 1) != bool(partial)
                or int.from_bytes(header[18:22], "little") != sequence
            ):
                raise MediaError("invalid_media")
            current_serial = header[14:18]
            if serial is not None and current_serial != serial:
                raise MediaError("unsupported_media_type", http_status=415)
            serial = current_serial
            lacing = stream.read(header[26])
            if not lacing or len(lacing) != header[26]:
                raise MediaError("invalid_media")
            content = stream.read(sum(lacing))
            if len(content) != sum(lacing):
                raise MediaError("invalid_media")
            checked = bytearray(header + lacing + content)
            checked[22:26] = b"\0" * 4
            if _ogg_crc(checked) != int.from_bytes(header[22:26], "little"):
                raise MediaError("invalid_media")
            offset = 0
            for length in lacing:
                partial.extend(content[offset : offset + length])
                offset += length
                if len(partial) > CHUNK_BYTES:
                    raise MediaError("invalid_media")
                if length < 255:
                    if (
                        native_opus
                        and packets == 1
                        and not partial.startswith(b"OpusTags")
                    ):
                        raise MediaError("invalid_media")
                    packets += 1
                    partial.clear()
            sequence += 1
            ended = bool(header[5] & 4)
    if not ended or partial or packets < (3 if native_opus else 2):
        raise MediaError("invalid_media")


@dataclass(frozen=True, slots=True)
class UploadReceipt:
    upload_id: str
    entry_id: str
    conversation_id: str
    principal_id: str
    kind: str
    mime_type: str
    file_name: str | None
    size_bytes: int
    sha256: str
    expires_at: float
    route_binding: str
    native_opus: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "status": "ready",
            "config_entry_id": self.entry_id,
            "conversation_id": self.conversation_id,
            "upload_id": self.upload_id,
            "kind": self.kind,
            "mime_type": self.mime_type,
            "file_name": self.file_name,
            "size_bytes": self.size_bytes,
            "sha256": self.sha256,
            "expires_at": _iso(self.expires_at),
        }


def _iso(value: float) -> str:
    return datetime.fromtimestamp(value, UTC).isoformat()


async def _drain(future: asyncio.Future) -> Any:
    """Finish owned work despite repeated caller cancellation, then propagate it."""
    cancelled = False
    while True:
        try:
            result = await asyncio.shield(future)
            break
        except asyncio.CancelledError:
            cancelled = True
            if future.done():
                future.result()
                break
        except BaseException:
            if cancelled:
                raise asyncio.CancelledError from None
            raise
    if cancelled:
        raise asyncio.CancelledError
    return result


async def _io(hass: Any, function: Callable, *args: Any) -> Any:
    """Drain each executor operation before transferring its resource ownership."""
    return await _drain(
        asyncio.ensure_future(hass.async_add_executor_job(function, *args))
    )


async def _finish_cleanup(cleanup: Awaitable[Any]) -> Any:
    """Drain a whole cleanup sequence, not merely its current executor call.

    The separate shielded task retains ownership through close, unlink, budget,
    lease, and deadline cleanup even when its caller is cancelled repeatedly.
    """
    return await _drain(asyncio.ensure_future(cleanup))


class _Store:
    def __init__(self, hass: Any):
        self.hass = hass
        self.path = Path(hass.config.path(".storage", "waha_whatsapp_media"))
        self.records: dict[str, dict[str, Any]] = {}
        self.secret = secrets.token_hex(32)
        self.lock = asyncio.Lock()
        self.started = False
        self.reservations = 0
        self.leases: dict[str, int] = {}
        self.timer: asyncio.TimerHandle | None = None

    def _load(self) -> None:
        self.path.mkdir(mode=0o700, parents=True, exist_ok=True)
        if self.path.is_symlink():
            raise OSError("unsafe media directory")
        os.chmod(self.path, 0o700)
        manifest = self.path / "index.json"
        if manifest.exists():
            if manifest.is_symlink() or manifest.stat().st_size > 8 * 1024 * 1024:
                raise OSError("invalid media index")
            data = json.loads(manifest.read_text())
            if not isinstance(data, dict) or not isinstance(data.get("records"), dict):
                raise OSError("invalid media index")
            self.secret = data["secret"]
            self.records = data["records"]
            if len(self.records) > REFERENCE_MAX_ITEMS or not isinstance(
                self.secret, str
            ):
                raise OSError("invalid media index")
        now = time.time()
        if not re.fullmatch(r"[a-f0-9]{64}", self.secret):
            raise OSError("invalid media index")
        total_bytes, total_files = 0, 0
        for key, record in list(self.records.items()):
            self._validate_record(key, record, now)
            name = record.get("file")
            if record["expires_at"] + RETENTION_SECONDS <= now:
                self.records.pop(key)
            elif record["expires_at"] <= now:
                record.pop("file", None)
            elif name:
                path = self.path / name
                if (
                    not path.is_file()
                    or path.is_symlink()
                    or path.stat().st_size != record["size_bytes"]
                ):
                    record.pop("file", None)
                else:
                    os.chmod(path, 0o600)
                    total_bytes += record["size_bytes"]
                    total_files += 1
        if total_bytes > CACHE_MAX_BYTES or total_files > CACHE_MAX_FILES:
            raise OSError("invalid media budget")
        owned = {r.get("file") for r in self.records.values()}
        for path in self.path.iterdir():
            if _FILE.fullmatch(path.name) and path.name not in owned:
                path.unlink()

    @staticmethod
    def _validate_record(key: str, record: object, now: float) -> None:
        if not isinstance(record, dict):
            raise OSError("invalid media index")
        kind = record.get("type")
        prefix = "att_" if kind == "inbound" else "upload_" if kind == "upload" else ""
        strings = ["entry_id", "conversation_id", "route_binding"]
        strings += (
            ["message_id", "raw_message_id", "raw_chat_id", "raw_from"]
            if kind == "inbound"
            else ["principal_id"]
        )
        expiry = record.get("expires_at")
        if (
            not prefix
            or not re.fullmatch(prefix + r"[a-f0-9]{32}", key)
            or any(
                not isinstance(record.get(field), str)
                or not 0 < len(record[field]) <= 2048
                for field in strings
            )
            or type(expiry) not in (int, float)
            or not math.isfinite(expiry)
            or expiry <= 0
            or expiry > now + RETENTION_SECONDS + 5
        ):
            raise OSError("invalid media index")
        for field in ("raw_participant", "participant_id"):
            value = record.get(field)
            if value is not None and (
                not isinstance(value, str) or not 0 < len(value) <= 512
            ):
                raise OSError("invalid media index")
        if kind == "inbound":
            metadata = record.get("metadata")
            if (
                not isinstance(metadata, dict)
                or metadata.get("kind") not in ("audio", "image", "file", "unknown")
                or metadata.get("mime_type") is not None
                and normalized_mime(metadata["mime_type"]) != metadata["mime_type"]
                or metadata.get("file_name") != display_name(metadata.get("file_name"))
                or metadata.get("voice_note") is not None
                and type(metadata["voice_note"]) is not bool
                or any(
                    metadata.get(field) is not None and _number(metadata[field]) is None
                    for field in ("size_bytes", "duration_ms")
                )
            ):
                raise OSError("invalid media index")
        if (record.get("file") or kind == "upload") and (
            record.get("file") is not None
            and (
                not isinstance(record["file"], str)
                or not _FILE.fullmatch(record["file"])
            )
            or type(record.get("size_bytes")) is not int
            or not 0 < record["size_bytes"] <= MAX_BYTES
            or not isinstance(record.get("sha256"), str)
            or not re.fullmatch(r"[a-f0-9]{64}", record["sha256"])
            or record.get("mime_type") not in MIME_TYPES
            or record.get("kind") not in ("audio", "image", "file")
            or type(record.get("native_opus")) is not bool
            or kind == "upload"
            and record.get("file_name") != display_name(record.get("file_name"))
        ):
            raise OSError("invalid media index")

    def _write(self) -> None:
        temporary = self.path / "index.tmp"
        descriptor = os.open(
            temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600
        )
        with os.fdopen(descriptor, "w") as stream:
            json.dump(
                {"secret": self.secret, "records": self.records},
                stream,
                separators=(",", ":"),
            )
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, self.path / "index.json")
        descriptor = os.open(self.path, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    async def persist(self) -> None:
        try:
            await _io(self.hass, self._write)
        except OSError as err:
            raise MediaError(
                "storage_unavailable", retryable=True, http_status=503
            ) from err
        self.schedule()

    def schedule(self) -> None:
        if self.timer:
            self.timer.cancel()
        future = [
            deadline
            for r in self.records.values()
            for deadline in (r["expires_at"], r["expires_at"] + RETENTION_SECONDS)
            if deadline > time.time()
        ]
        if future:
            self.timer = asyncio.get_running_loop().call_later(
                max(0, min(future) - time.time()),
                lambda: asyncio.create_task(self.cleanup()),
            )

    async def cleanup(self) -> None:
        await _finish_cleanup(self._cleanup())

    async def _cleanup(self) -> None:
        async with self.lock:
            for key, record in list(self.records.items()):
                if record["expires_at"] <= time.time() and not self.leases.get(key):
                    if name := record.pop("file", None):
                        await _io(self.hass, (self.path / name).unlink, True)
                    # Keep bounded tombstones briefly for accurate expiry errors.
                    if record["expires_at"] + RETENTION_SECONDS <= time.time():
                        self.records.pop(key)
            await self.persist()


class MediaManager:
    """One entry's media layer over the integration-wide private store."""

    def __init__(
        self,
        hass: Any,
        entry: Any,
        client: Any,
        *,
        authorize: Callable[..., Awaitable[str]],
        backend_ready: Callable[[], Awaitable[bool]],
        observe_binding: Callable[..., str | None] | None = None,
    ):
        self.hass, self.entry, self.client = hass, entry, client
        self.authorize, self.backend_ready = authorize, backend_ready
        self.observe_binding = observe_binding
        self.store = hass.data.setdefault(STORE_KEY, _Store(hass))
        self.enabled = False
        self._fetches: dict[str, asyncio.Task] = {}
        self._fetch_lock, self._upload_lock = asyncio.Lock(), asyncio.Lock()
        self._waiting = {"fetch": 0, "upload": 0}
        self._get_waiters = 0
        self._operations: set[asyncio.Task] = set()

    @property
    def capabilities(self) -> dict[str, Any]:
        return {
            "version": 1,
            "inbound_kinds": ["audio", "image", "file"],
            "outbound_kinds": ["audio", "image", "file"],
            "voice_note_detection": True,
            "voice_note_send": True,
            "max_download_bytes": MAX_BYTES,
            "max_upload_bytes": MAX_BYTES,
            "cache_max_bytes": CACHE_MAX_BYTES,
            "cache_max_files": CACHE_MAX_FILES,
            "cache_retention_seconds": RETENTION_SECONDS,
            "reference_retention_seconds": RETENTION_SECONDS,
            "reference_max_items": REFERENCE_MAX_ITEMS,
            "mime_types": list(MIME_TYPES),
            "caption_max_length": 1024,
            "caption_kinds": ["image", "file", "audio"],
            "voice_note_captions": False,
            "voice_note_mime_type": "audio/ogg; codecs=opus",
            "voice_note_conversion": False,
            "voice_note_channels": [1, 2],
            "backend_fetch_concurrency_per_entry": 1,
            "upload_concurrency_per_entry": 1,
            "transfer_queue_per_entry": QUEUE_MAX,
            "send_concurrency_global": 1,
            "send_queue_global": QUEUE_MAX,
            "connection_timeout_seconds": 10,
            "transfer_timeout_seconds": 60,
        }

    @property
    def diagnostics(self) -> dict[str, Any]:
        records = [
            r
            for r in self.store.records.values()
            if r["entry_id"] == self.entry.entry_id
        ]
        return {
            "enabled": self.enabled,
            "references": len(records),
            "cache_files": sum(bool(r.get("file")) for r in records),
            "cache_bytes": sum(
                r.get("size_bytes", 0) for r in records if r.get("file")
            ),
            "fetches": len(self._fetches),
            "waiting": dict(self._waiting),
        }

    async def async_start(self) -> None:
        async with self.store.lock:
            if not self.store.started:
                try:
                    await _io(self.hass, self.store._load)
                except (OSError, ValueError, KeyError, TypeError) as err:
                    raise MediaError("storage_unavailable", http_status=503) from err
                self.store.started = True
                await self.store.persist()
        self.enabled = True

    async def async_stop(self) -> None:
        self.enabled = False
        tasks = list(set(self._fetches.values()) | self._operations)
        tasks = [task for task in tasks if task is not asyncio.current_task()]
        for task in tasks:
            task.cancel()
        await _finish_cleanup(asyncio.gather(*tasks, return_exceptions=True))

    async def async_remove_entry(self) -> None:
        """Delete this entry's owned state, after unload has stopped operations."""
        await _finish_cleanup(self._remove_entry_state())

    async def _remove_entry_state(self) -> None:
        await self.async_stop()
        async with self.store.lock:
            for identifier, record in list(self.store.records.items()):
                if record["entry_id"] != self.entry.entry_id:
                    continue
                if self.store.leases.get(identifier):
                    record["expires_at"] = time.time()
                    continue
                if name := record.get("file"):
                    await _io(self.hass, (self.store.path / name).unlink, True)
                self.store.records.pop(identifier)
            await self.store.persist()

    async def _route(self, conversation_id: str) -> str:
        if not self.enabled:
            raise MediaError("media_disabled", http_status=503)
        binding = await self.authorize(conversation_id)
        if not isinstance(binding, str) or not binding:
            raise MediaError("invalid_conversation", http_status=404)
        return binding

    def capture_observation_binding(
        self, conversation_id: str, *, raw_participant: str | None = None
    ) -> str:
        """Capture already-verified callback identity before persistence can yield."""
        if not self.enabled or self.observe_binding is None:
            return ""
        return (
            self.observe_binding(conversation_id, raw_participant=raw_participant) or ""
        )

    async def async_observe(
        self,
        payload: Mapping[str, Any],
        message_id: str,
        conversation_id: str,
        *,
        raw_chat_id: str | None = None,
        route_binding: str | None = None,
        participant_id: str | None = None,
        raw_participant: str | None = None,
    ) -> list[dict[str, Any]]:
        metadata = media_metadata(payload)
        attachment_id = (
            "att_"
            + hmac.new(
                self.store.secret.encode(),
                f"{self.entry.entry_id}\0{conversation_id}\0{message_id}".encode(),
                hashlib.sha256,
            ).hexdigest()[:32]
        )
        result = {"attachment_id": attachment_id, **metadata, "retrievable": False}
        if not self.enabled:
            return [result]
        try:
            current_binding = self.capture_observation_binding(
                conversation_id,
                raw_participant=raw_participant or payload.get("participant"),
            )
            binding = route_binding if route_binding is not None else current_binding
            if (
                not binding
                or self.observe_binding is not None
                and binding != current_binding
            ):
                return [result]
            async with self.store.lock:
                existing = self.store.records.get(attachment_id)
                if existing is None:
                    if len(self.store.records) >= REFERENCE_MAX_ITEMS:
                        return [result]
                    self.store.records[attachment_id] = {
                        "type": "inbound",
                        "entry_id": self.entry.entry_id,
                        "conversation_id": conversation_id,
                        "message_id": message_id,
                        "raw_message_id": payload["id"],
                        "raw_chat_id": raw_chat_id
                        or payload.get("chatId")
                        or payload["from"],
                        "raw_participant": raw_participant
                        or payload.get("participant"),
                        "raw_from": payload.get("from"),
                        "participant_id": participant_id,
                        "route_binding": binding,
                        "expires_at": time.time() + RETENTION_SECONDS,
                        "metadata": metadata,
                    }
                    try:
                        await self.store.persist()
                    except BaseException:
                        self.store.records.pop(attachment_id, None)
                        raise
                elif (
                    existing["expires_at"] <= time.time()
                    or existing["entry_id"] != self.entry.entry_id
                    or existing["conversation_id"] != conversation_id
                    or existing["message_id"] != message_id
                    or existing["raw_message_id"] != payload["id"]
                    or existing["raw_chat_id"]
                    != (raw_chat_id or payload.get("chatId") or payload["from"])
                    or existing.get("raw_from") != payload.get("from")
                    or existing.get("raw_participant")
                    != (raw_participant or payload.get("participant"))
                    or existing.get("participant_id") != participant_id
                    or existing["route_binding"] != binding
                    or existing["metadata"] != metadata
                ):
                    return [result]
                result["retrievable"] = True
        except MediaError:
            pass
        return [result]

    def _record(
        self,
        identifier: str,
        conversation_id: str,
        *,
        message_id: str | None = None,
        record_type: str = "inbound",
        allow_expired: bool = False,
    ) -> dict[str, Any]:
        record = self.store.records.get(identifier)
        if (
            not record
            or record["entry_id"] != self.entry.entry_id
            or record["conversation_id"] != conversation_id
            or record["type"] != record_type
            or message_id is not None
            and record.get("message_id") != message_id
        ):
            raise MediaError("media_not_found", http_status=404)
        if not allow_expired and record["expires_at"] <= time.time():
            raise MediaError("media_expired", http_status=410)
        return record

    async def _validate(self, record: dict[str, Any]) -> None:
        if not self.enabled:
            raise MediaError("media_disabled", http_status=503)
        binding = await self.authorize(
            record["conversation_id"], raw_participant=record.get("raw_participant")
        )
        if binding != record["route_binding"]:
            raise MediaError("media_not_found", http_status=404)

    @asynccontextmanager
    async def _slot(self, operation: str) -> AsyncIterator[None]:
        if self._waiting[operation] >= QUEUE_MAX + 1:
            raise MediaError("busy", retryable=True, http_status=429)
        self._waiting[operation] += 1
        try:
            async with self._fetch_lock if operation == "fetch" else self._upload_lock:
                if not self.enabled:
                    raise MediaError("media_disabled")
                yield
        finally:
            self._waiting[operation] -= 1

    @asynccontextmanager
    async def _operation(self):
        task = asyncio.current_task()
        self._operations.add(task)
        try:
            if not self.enabled:
                raise MediaError("media_disabled")
            yield
        finally:
            self._operations.discard(task)

    async def _store_stream(
        self,
        record: dict[str, Any],
        chunks: AsyncIterator[bytes],
        max_bytes: int,
        declared_type: str | None,
    ) -> None:
        async with self.store.lock:
            files = [r for r in self.store.records.values() if r.get("file")]
            if (
                len(files) + self.store.reservations >= CACHE_MAX_FILES
                or sum(r["size_bytes"] for r in files)
                + (self.store.reservations + 1) * MAX_BYTES
                > CACHE_MAX_BYTES
            ):
                raise MediaError("storage_unavailable", retryable=True, http_status=503)
            self.store.reservations += 1
        name = secrets.token_hex(16) + ".bin"
        path = self.store.path / name
        stream = None
        success = False
        try:

            def create_file():
                nonlocal stream
                stream = os.fdopen(
                    os.open(
                        path,
                        os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                        0o600,
                    ),
                    "wb",
                )

            await _io(self.hass, create_file)
            size, digest, head = 0, hashlib.sha256(), bytearray()
            async with asyncio.timeout(60):
                async for chunk in chunks:
                    if not isinstance(chunk, bytes) or len(chunk) > CHUNK_BYTES:
                        raise MediaError("invalid_media")
                    size += len(chunk)
                    if size > max_bytes:
                        raise MediaError("media_too_large", http_status=413)
                    digest.update(chunk)
                    if len(head) < 4096:
                        head.extend(chunk[: 4096 - len(head)])
                    await _io(self.hass, stream.write, chunk)
            if size == 0:
                raise MediaError("invalid_media")
            kind, mime, native_opus = sniff_media(bytes(head))
            if declared_type and declared_type != mime:
                raise MediaError("invalid_media", http_status=415)

            def sync_file():
                stream.flush()
                os.fsync(stream.fileno())

            await _io(self.hass, sync_file)
            await _io(self.hass, stream.close)
            stream = None
            if mime == "audio/ogg":
                await _io(self.hass, _validate_ogg, path, native_opus)
            record.update(
                file=name,
                size_bytes=size,
                sha256=digest.hexdigest(),
                mime_type=mime,
                kind=kind,
                native_opus=native_opus,
            )
            success = True
        except TimeoutError as err:
            raise MediaError(
                "transport_failure", retryable=True, http_status=504
            ) from err
        except OSError as err:
            raise MediaError(
                "storage_unavailable", retryable=True, http_status=503
            ) from err
        finally:
            if not success:

                async def cleanup_partial() -> None:
                    try:
                        if stream:
                            await _io(self.hass, stream.close)
                    finally:
                        try:
                            await _io(self.hass, path.unlink, True)
                        finally:
                            self.store.reservations -= 1

                await _finish_cleanup(cleanup_partial())

    async def _fetch(
        self, identifier: str, record: dict[str, Any], max_bytes: int
    ) -> None:
        from .media_backend import async_download

        async with self._slot("fetch"):
            await self._validate(record)
            if record["expires_at"] <= time.time():
                raise MediaError("media_expired")
            if not await self.backend_ready():
                raise MediaError("channel_unavailable", retryable=True, http_status=503)
            working = dict(record)
            stored = False
            self.store.leases[identifier] = self.store.leases.get(identifier, 0) + 1
            try:
                async with async_download(self.client, record) as (metadata, chunks):
                    await self._store_stream(
                        working, chunks, max_bytes, metadata["mime_type"]
                    )
                    stored = True
                await self._validate(record)
                if record["expires_at"] <= time.time():
                    raise MediaError("media_expired", http_status=410)
                async with self.store.lock:
                    record.update(working)
                    try:
                        await self.store.persist()
                    except BaseException:
                        record.pop("file", None)
                        raise
                stored = False
            finally:

                async def cleanup_fetch() -> None:
                    try:
                        if stored:
                            await _io(
                                self.hass,
                                (self.store.path / working["file"]).unlink,
                                True,
                            )
                    finally:
                        if working.get("file"):
                            self.store.reservations -= 1
                        self.store.leases[identifier] -= 1
                        if not self.store.leases[identifier]:
                            self.store.leases.pop(identifier)
                        if record["expires_at"] <= time.time():
                            await self.store.cleanup()

                await _finish_cleanup(cleanup_fetch())

    async def async_get_media(
        self,
        conversation_id: str,
        message_id: str,
        attachment_id: str,
        max_bytes: int | None = None,
    ) -> dict[str, Any]:
        if max_bytes is None:
            max_bytes = MAX_BYTES
        if type(max_bytes) is not int or not 0 < max_bytes <= MAX_BYTES:
            raise MediaError("invalid_request")
        await self._route(conversation_id)
        record = self._record(attachment_id, conversation_id, message_id=message_id)
        announced_size = record["metadata"].get("size_bytes")
        if type(announced_size) is int and announced_size > max_bytes:
            raise MediaError("media_too_large", http_status=413)
        await self._validate(record)
        if not record.get("file"):
            if self._get_waiters >= QUEUE_MAX + 1:
                raise MediaError("busy", retryable=True, http_status=429)
            if attachment_id not in self._fetches:
                if len(self._fetches) >= QUEUE_MAX + 1:
                    raise MediaError("busy", retryable=True, http_status=429)
                task = asyncio.create_task(
                    self._fetch(attachment_id, record, max_bytes)
                )
                self._fetches[attachment_id] = task
                task.add_done_callback(
                    lambda done: (
                        self._fetches.pop(attachment_id, None),
                        None if done.cancelled() else done.exception(),
                    )
                )
            self._get_waiters += 1
            try:
                await asyncio.shield(self._fetches[attachment_id])
            finally:
                self._get_waiters -= 1
        await self._validate(record)
        self._record(attachment_id, conversation_id, message_id=message_id)
        if record["size_bytes"] > max_bytes:
            raise MediaError("media_too_large", http_status=413)
        metadata = {
            **record["metadata"],
            "attachment_id": attachment_id,
            "mime_type": record["mime_type"],
            "size_bytes": record["size_bytes"],
            "sha256": record["sha256"],
        }
        return {
            "status": "ready",
            "config_entry_id": self.entry.entry_id,
            "conversation_id": conversation_id,
            "message_id": message_id,
            "attachment": metadata,
            "download": {
                "method": "GET",
                "path": (
                    f"/api/waha_whatsapp/{self.entry.entry_id}/conversations/"
                    f"{conversation_id}/media/{attachment_id}"
                ),
                "requires_ha_auth": True,
                "expires_at": _iso(record["expires_at"]),
            },
        }

    async def async_stage(
        self,
        conversation_id: str,
        principal_id: str,
        chunks: AsyncIterator[bytes],
        declared_type: str | None,
        file_name: str | None,
        *,
        authorize_principal: Callable[[str], Awaitable[bool]] | None = None,
    ) -> UploadReceipt:
        binding = await self._route(conversation_id)
        if (
            not principal_id
            or authorize_principal is not None
            and not await authorize_principal(principal_id)
        ):
            raise MediaError("not_authorized", http_status=403)
        mime = normalized_mime(declared_type)
        if mime not in MIME_TYPES:
            raise MediaError("unsupported_media_type", http_status=415)
        async with self._operation(), self._slot("upload"):
            record = {
                "type": "upload",
                "entry_id": self.entry.entry_id,
                "conversation_id": conversation_id,
                "principal_id": principal_id,
                "route_binding": binding,
                "expires_at": time.time() + RETENTION_SECONDS,
                "file_name": display_name(file_name),
            }
            async with self.store.lock:
                if len(self.store.records) >= REFERENCE_MAX_ITEMS:
                    raise MediaError("busy", retryable=True, http_status=429)
            await self._store_stream(record, chunks, MAX_BYTES, mime)
            upload_id = "upload_" + secrets.token_hex(16)
            committed = False
            try:
                await self._validate(record)
                if record["expires_at"] <= time.time():
                    raise MediaError("media_expired", http_status=410)
                async with self.store.lock:
                    if len(self.store.records) >= REFERENCE_MAX_ITEMS:
                        raise MediaError("busy", retryable=True, http_status=429)
                    if (
                        authorize_principal is not None
                        and not await authorize_principal(principal_id)
                    ):
                        raise MediaError("not_authorized", http_status=403)
                    self.store.records[upload_id] = record
                    await self.store.persist()
                    committed = True
            finally:
                if committed:
                    self.store.reservations -= 1
                else:

                    async def cleanup_upload() -> None:
                        try:
                            async with self.store.lock:
                                self.store.records.pop(upload_id, None)
                                await _io(
                                    self.hass,
                                    (self.store.path / record["file"]).unlink,
                                    True,
                                )
                                await self.store.persist()
                        finally:
                            self.store.reservations -= 1

                    await _finish_cleanup(cleanup_upload())
        return self.get_upload(upload_id, conversation_id, principal_id)

    def get_upload(
        self,
        upload_id: str,
        conversation_id: str,
        principal_id: str,
        *,
        allow_expired: bool = False,
    ) -> UploadReceipt:
        record = self._record(
            upload_id,
            conversation_id,
            record_type="upload",
            allow_expired=allow_expired,
        )
        if record["principal_id"] != principal_id:
            raise MediaError("media_not_found", http_status=404)
        return UploadReceipt(
            upload_id=upload_id,
            **{
                key: record[key]
                for key in UploadReceipt.__dataclass_fields__
                if key != "upload_id"
            },
        )

    @asynccontextmanager
    async def _open(
        self, identifier: str, record: dict[str, Any]
    ) -> AsyncIterator[Path]:
        await self._validate(record)
        if record["expires_at"] <= time.time():
            raise MediaError("media_expired", http_status=410)
        name = record.get("file")
        if not name or not _FILE.fullmatch(name):
            raise MediaError("media_not_found", http_status=404)
        self.store.leases[identifier] = self.store.leases.get(identifier, 0) + 1
        try:
            path = self.store.path / name

            def check() -> bool:
                with os.fdopen(
                    os.open(path, os.O_RDONLY | os.O_NOFOLLOW), "rb"
                ) as stream:
                    size, digest = 0, hashlib.sha256()
                    while chunk := stream.read(CHUNK_BYTES):
                        size += len(chunk)
                        if size > MAX_BYTES:
                            return False
                        digest.update(chunk)
                    return (
                        size == record["size_bytes"]
                        and digest.hexdigest() == record["sha256"]
                    )

            try:
                if not await _io(self.hass, check):
                    raise MediaError("invalid_media")
            except OSError as err:
                raise MediaError("media_not_found", http_status=404) from err
            await self._validate(record)
            yield path
        finally:

            async def cleanup_lease() -> None:
                self.store.leases[identifier] -= 1
                if not self.store.leases[identifier]:
                    self.store.leases.pop(identifier)
                if record["expires_at"] <= time.time():
                    await self.store.cleanup()

            await _finish_cleanup(cleanup_lease())

    @asynccontextmanager
    async def async_open_upload(
        self, upload_id: str, conversation_id: str, principal_id: str
    ) -> AsyncIterator[tuple[UploadReceipt, Path]]:
        receipt = self.get_upload(upload_id, conversation_id, principal_id)
        record = self._record(upload_id, conversation_id, record_type="upload")
        async with self._operation(), self._open(upload_id, record) as path:
            yield receipt, path

    def validate_download_reference(
        self, conversation_id: str, attachment_id: str
    ) -> None:
        """Fence local expiry/config changes immediately before publishing bytes."""
        if not self.enabled:
            raise MediaError("media_disabled")
        record = self._record(attachment_id, conversation_id)
        if self.observe_binding is not None:
            binding = self.observe_binding(
                conversation_id, raw_participant=record.get("raw_participant")
            )
            if binding != record["route_binding"]:
                raise MediaError("media_not_found")

    async def async_validate_download(
        self, conversation_id: str, attachment_id: str, stream: Any
    ) -> None:
        """Validate the actual opened descriptor, then the fresh route and expiry.

        An earlier pathname check cannot establish the contents of a later open.
        Reads stay bounded even if another process tampers with a private file.
        """
        record = self._record(attachment_id, conversation_id)

        def check_stream() -> bool:
            if os.fstat(stream.fileno()).st_size != record["size_bytes"]:
                return False
            size, digest = 0, hashlib.sha256()
            while chunk := stream.read(CHUNK_BYTES):
                size += len(chunk)
                if size > MAX_BYTES or size > record["size_bytes"]:
                    return False
                digest.update(chunk)
            stream.seek(0)
            return (
                size == record["size_bytes"] and digest.hexdigest() == record["sha256"]
            )

        try:
            if not await _io(self.hass, check_stream):
                raise MediaError("invalid_media")
        except OSError as err:
            raise MediaError("media_not_found") from err
        await self._validate(record)
        self.validate_download_reference(conversation_id, attachment_id)

    @asynccontextmanager
    async def async_download(
        self, conversation_id: str, attachment_id: str
    ) -> AsyncIterator[tuple[dict[str, Any], Path]]:
        await self._route(conversation_id)
        record = self._record(attachment_id, conversation_id)
        async with self._operation(), self._open(attachment_id, record) as path:
            yield (
                {
                    "mime_type": record["mime_type"],
                    "file_name": record["metadata"]["file_name"],
                    "size_bytes": record["size_bytes"],
                },
                path,
            )
