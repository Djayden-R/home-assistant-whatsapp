"""Correlated generic media sends over explicitly authorized conversations."""

from __future__ import annotations

import asyncio
import hashlib
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from contextlib import asynccontextmanager
from typing import Any

from .correlated import MAX_CLIENT_REQUEST_ID_LENGTH, CorrelatedSendError
from .media_backend_send import (
    MAX_CAPTION_LENGTH,
    async_prepare_staged_media,
    async_send_staged_media,
)
from .outbound import async_send_observed

_ORIGINS = {"automation", "application", "human", "unknown"}
_FIELDS = {
    "config_entry_id",
    "conversation_id",
    "upload_id",
    "kind",
    "as_voice_note",
    "client_request_id",
    "origin",
    "caption",
    "reply_to_message_id",
    "content_redacted",
}


@asynccontextmanager
async def _send_slot(manager: Any) -> AsyncIterator[None]:
    """Bound private base64 preparation to one global send and four waiters."""
    gate = getattr(manager, "store", manager)
    if not hasattr(gate, "_media_send_lock"):
        gate._media_send_lock = asyncio.Lock()
        gate._media_send_waiting = 0
    if gate._media_send_lock.locked() and gate._media_send_waiting >= 4:
        raise CorrelatedSendError("busy")
    gate._media_send_waiting += 1
    acquired = False
    try:
        await gate._media_send_lock.acquire()
        acquired = True
        gate._media_send_waiting -= 1
        yield
    finally:
        if acquired:
            gate._media_send_lock.release()
        else:
            gate._media_send_waiting -= 1


def _token(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _binding(receipt: Any, principal_id: str) -> dict[str, Any]:
    """Persist content facts and hashed scope, never names or upload secrets."""
    return {
        "upload_tokens": [_token(receipt.upload_id)],
        "principal_token": _token(principal_id),
        "sha256": receipt.sha256,
        "size_bytes": receipt.size_bytes,
        "kind": receipt.kind,
        "mime_type": receipt.mime_type,
        "native_opus": receipt.native_opus,
        "file_name_digest": _token(receipt.file_name or ""),
        "route_binding_digest": _token(receipt.route_binding),
    }


def _failure(data: Mapping[str, Any], code: str) -> dict[str, Any]:
    return {
        "conversation_id": data.get("conversation_id"),
        "client_request_id": data.get("client_request_id"),
        "status": "failed",
        "message_id": None,
        "error_code": code,
        "replayed": False,
    }


def _valid_request(data: Mapping[str, Any], entry_id: str) -> bool:
    return (
        not set(data) - _FIELDS
        and data.get("config_entry_id") == entry_id
        and all(
            isinstance(data.get(field), str) and 1 <= len(data[field]) <= maximum
            for field, maximum in (
                ("conversation_id", 256),
                ("upload_id", 256),
                ("client_request_id", MAX_CLIENT_REQUEST_ID_LENGTH),
            )
        )
        and isinstance(data.get("kind"), str)
        and data["kind"] in {"audio", "image", "file"}
        and type(data.get("as_voice_note", False)) is bool
        and (not data.get("as_voice_note") or data["kind"] == "audio")
        and type(data.get("content_redacted", False)) is bool
        and isinstance(data.get("origin", "unknown"), str)
        and data.get("origin", "unknown") in _ORIGINS
        and (
            data.get("caption") is None
            or (
                isinstance(data["caption"], str)
                and len(data["caption"]) <= MAX_CAPTION_LENGTH
            )
        )
        and (
            data.get("reply_to_message_id") is None
            or (
                isinstance(data["reply_to_message_id"], str)
                and 1 <= len(data["reply_to_message_id"]) <= 256
            )
        )
    )


async def async_send_media(
    entry: Any,
    principal_id: str,
    data: Mapping[str, Any],
    *,
    async_route: Callable[[str, Callable[..., Awaitable[Any]]], Awaitable[Any]],
) -> dict[str, Any]:
    """Send one staged file, or replay its durable outcome without file I/O.

    The HA entrypoint must authenticate an active administrator and supply its
    actual user ID as principal_id. async_route validates the exact current
    destination, then invokes transmit(chat_id, conversation_type, redacted),
    retaining any guest-route lock until that callback returns. It is called
    before receipt replay and again immediately before each new transmission.
    """
    if not isinstance(data, Mapping) or not _valid_request(data, entry.entry_id):
        return _failure(data if isinstance(data, Mapping) else {}, "invalid_request")
    if not isinstance(principal_id, str) or not principal_id:
        return _failure(data, "not_authorized")
    runtime = entry.runtime_data
    manager = getattr(runtime, "media_manager", None)
    if manager is None or not manager.enabled:
        return _failure(data, "media_disabled")
    conversation = data["conversation_id"]
    request_id = data["client_request_id"]
    upload_id = data["upload_id"]
    ledger = runtime.correlated_sends
    try:

        async def check_route(*_args: Any) -> None:
            return None

        await async_route(conversation, check_route)
        # Resolve only trusted exact-scope receipts. Expired metadata is useful
        # solely to compare a durable request; byte expiry is checked on send.
        try:
            receipt = manager.get_upload(
                upload_id, conversation, principal_id, allow_expired=True
            )
            binding = _binding(receipt, principal_id)
        except Exception as err:
            binding = ledger.media_binding(
                conversation, request_id, _token(upload_id), _token(principal_id)
            )
            if binding is None:
                code = getattr(err, "code", "media_not_found")
                return _failure(data, code)
        kind = data["kind"]
        voice = data.get("as_voice_note", False)
        canonical = {
            "operation": "media",
            **{key: value for key, value in binding.items() if key != "upload_tokens"},
            "kind": kind,
            "as_voice_note": voice,
            "caption": data.get("caption"),
            "reply_to_message_id": data.get("reply_to_message_id"),
            "origin": data.get("origin", "unknown"),
            "content_redacted": data.get("content_redacted", False),
        }

        async def send() -> dict[str, Any]:
            if kind != binding["kind"]:
                raise CorrelatedSendError("unsupported_media_type")
            if voice and (
                not binding["native_opus"] or data.get("caption") is not None
            ):
                raise CorrelatedSendError("unsupported")
            quote = data.get("reply_to_message_id")
            raw_quote = None
            if quote is not None:
                raw_quote = runtime.channel_registry.resolve_message(
                    quote, conversation
                )
                if raw_quote is None:
                    raise CorrelatedSendError("invalid_reply_target")
            try:
                async with (
                    _send_slot(manager),
                    manager.async_open_upload(
                        upload_id, conversation, principal_id
                    ) as (current, path),
                ):
                    if _binding(current, principal_id) != binding:
                        raise CorrelatedSendError("invalid_media")
                    try:
                        prepared = await async_prepare_staged_media(
                            runtime.client,
                            path,
                            current,
                            kind=kind,
                            as_voice_note=voice,
                            caption=data.get("caption"),
                            reply_to_message_id=raw_quote,
                        )
                    except ValueError as err:
                        raise CorrelatedSendError("invalid_media") from err
                    except OSError as err:
                        raise CorrelatedSendError("storage_unavailable") from err
                    # Preparation can await executor work. Recheck expiry and
                    # permissions afterwards, immediately before transmission.
                    manager.get_upload(upload_id, conversation, principal_id)

                    async def transmit(
                        chat_id: str,
                        conversation_type: str,
                        redacted: bool,
                    ) -> dict[str, Any]:
                        async def backend_send() -> Any:
                            try:
                                return await async_send_staged_media(
                                    runtime.client,
                                    path,
                                    current,
                                    chat_id,
                                    kind=kind,
                                    as_voice_note=voice,
                                    caption=data.get("caption"),
                                    reply_to_message_id=raw_quote,
                                    prepared_payload=prepared,
                                )
                            except ValueError as err:
                                code = (
                                    "media_expired"
                                    if str(err) == "media_expired"
                                    else "invalid_media"
                                )
                                raise CorrelatedSendError(code) from err

                        result = await async_send_observed(
                            entry,
                            conversation,
                            conversation_type,
                            {
                                "kind": kind,
                                "in_reply_to": quote,
                                "caption": data.get("caption"),
                                "attachments": [
                                    {
                                        "kind": kind,
                                        "mime_type": current.mime_type,
                                        "size_bytes": current.size_bytes,
                                        "sha256": current.sha256,
                                        "file_name": current.file_name,
                                        "voice_note": voice,
                                    }
                                ],
                            },
                            backend_send,
                            origin=data.get("origin", "unknown"),
                            client_request_id=request_id,
                            content_redacted=redacted
                            or data.get("content_redacted", False),
                        )
                        message_id = None
                        if result.id is not None:
                            try:
                                opaque = runtime.channel_registry.message_token(
                                    result.id
                                )
                                if runtime.channel_registry.resolve_message(
                                    opaque, conversation
                                ):
                                    message_id = opaque
                            except Exception:
                                pass
                        return {"status": result.outcome, "message_id": message_id}

                    return await async_route(conversation, transmit)
            except CorrelatedSendError:
                raise
            except Exception as err:
                code = getattr(err, "code", None)
                if code is not None:
                    raise CorrelatedSendError(code) from err
                raise

        return await ledger.async_execute(
            conversation, request_id, canonical, send, media_binding=binding
        )
    except CorrelatedSendError as err:
        return _failure(data, err.code)
    except Exception as err:
        code = getattr(err, "code", None)
        if code is not None:
            return _failure(data, code)
        return _failure(data, "channel_unavailable")
