"""Private bounded WAHA media submission; no public fetch URLs are created."""

from __future__ import annotations

import asyncio
import base64
import json
import os
import stat
import time
from pathlib import Path
from typing import Any

from aiohttp import ClientError, ClientTimeout

from .api import (
    WahaAuthenticationError,
    WahaConnectionError,
    WahaMessage,
    WahaRequestError,
    WahaResponseError,
    _message_id,
    _valid_group_id,
    _valid_participant_id,
)

MAX_SEND_BYTES = 25 * 1024 * 1024
MAX_SEND_RESPONSE_BYTES = 64 * 1024
MAX_CAPTION_LENGTH = 1024


def _payload(
    path: Path,
    receipt: Any,
    *,
    session: str,
    chat_id: str,
    kind: str,
    as_voice_note: bool,
    caption: str | None,
    reply_to: str | None,
) -> dict[str, Any]:
    """Read bounded private bytes and encode away from the HA event loop."""
    import hashlib

    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(descriptor, "rb") as stream:
            if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                raise ValueError("invalid_media")
            content = stream.read(MAX_SEND_BYTES + 1)
    except OSError as err:
        raise ValueError("invalid_media") from err
    if (
        not content
        or len(content) > MAX_SEND_BYTES
        or len(content) != receipt.size_bytes
        or hashlib.sha256(content).hexdigest() != receipt.sha256
    ):
        raise ValueError("invalid_media")
    file = {
        "mimetype": "audio/ogg; codecs=opus" if as_voice_note else receipt.mime_type,
        "filename": receipt.file_name
        or ("voice.ogg" if as_voice_note else "attachment"),
        "data": base64.b64encode(content).decode("ascii"),
    }
    result = {"session": session, "chatId": chat_id, "file": file}
    if as_voice_note:
        result["convert"] = False
    if caption is not None:
        result["caption"] = caption
    if reply_to is not None:
        result["reply_to"] = reply_to
    return result


async def async_prepare_staged_media(
    client: Any,
    path: Path,
    receipt: Any,
    *,
    kind: str,
    as_voice_note: bool,
    caption: str | None,
    reply_to_message_id: str | None,
) -> dict[str, Any]:
    """Prepare private bytes before the caller takes its final route guard."""
    preparing = asyncio.create_task(
        asyncio.to_thread(
            _payload,
            path,
            receipt,
            session=client.session_name,
            chat_id="",
            kind=kind,
            as_voice_note=as_voice_note,
            caption=caption,
            reply_to=reply_to_message_id,
        )
    )
    cancelled = False
    while True:
        try:
            payload = await asyncio.shield(preparing)
            break
        except asyncio.CancelledError:
            if preparing.cancelled():
                raise
            cancelled = True
    if cancelled:
        raise asyncio.CancelledError
    return payload


async def async_send_staged_media(
    client: Any,
    path: Path,
    receipt: Any,
    chat_id: str,
    *,
    kind: str,
    as_voice_note: bool = False,
    caption: str | None = None,
    reply_to_message_id: str | None = None,
    prepared_payload: dict[str, Any] | None = None,
) -> WahaMessage:
    """Submit one authenticated private payload, without redirects or retries.

    Ordinary audio uses sendFile. Native voice is verified Ogg/Opus and uses
    sendVoice with conversion explicitly disabled. No converter is invoked.
    """
    if not (_valid_participant_id(chat_id) or _valid_group_id(chat_id)):
        raise ValueError("invalid_conversation")
    if kind not in {"audio", "image", "file"} or receipt.kind != kind:
        raise ValueError("unsupported_media_type")
    if as_voice_note and (kind != "audio" or not receipt.native_opus):
        raise ValueError("unsupported")
    if caption is not None and (
        not isinstance(caption, str)
        or len(caption) > MAX_CAPTION_LENGTH
        or as_voice_note
    ):
        raise ValueError("unsupported")
    payload = prepared_payload
    if payload is None:
        payload = await async_prepare_staged_media(
            client,
            path,
            receipt,
            kind=kind,
            as_voice_note=as_voice_note,
            caption=caption,
            reply_to_message_id=reply_to_message_id,
        )
    if getattr(receipt, "expires_at", float("inf")) <= time.time():
        raise ValueError("media_expired")
    payload = {**payload, "session": client.session_name, "chatId": chat_id}
    endpoint = (
        "sendVoice"
        if as_voice_note
        else ("sendImage" if kind == "image" else "sendFile")
    )
    try:
        async with client._session.request(
            "POST",
            f"{client.base_url}/api/{endpoint}",
            headers={
                "X-Api-Key": client._api_key,
                "Accept": "application/json",
                "Accept-Encoding": "identity",
            },
            json=payload,
            allow_redirects=False,
            auto_decompress=False,
            timeout=ClientTimeout(total=60, connect=10),
        ) as response:
            if response.status in (401, 403):
                raise WahaAuthenticationError("WAHA authentication rejected")
            if response.status >= 400:
                raise WahaRequestError("WAHA media request rejected", response.status)
            if response.status < 200 or response.status >= 300:
                raise WahaResponseError("WAHA media response uncertain")
            if (
                response.headers.get("Content-Encoding", "identity").lower()
                != "identity"
            ):
                raise WahaResponseError("WAHA media response encoding unsupported")
            chunks = bytearray()
            async for chunk in response.content.iter_chunked(8192):
                chunks.extend(chunk)
                if len(chunks) > MAX_SEND_RESPONSE_BYTES:
                    raise WahaResponseError("WAHA media response exceeds limits")
            try:
                data = json.loads(chunks)
            except (ValueError, TypeError) as err:
                raise WahaResponseError("WAHA media response uncertain") from err
    except (ClientError, OSError, TimeoutError) as err:
        raise WahaConnectionError("WAHA media outcome uncertain") from err
    # A malformed success or explicit false is uncertain: never imply that
    # another submission is safe merely because the response omitted an ID.
    outcome = (
        "accepted"
        if isinstance(data, dict) and data.get("success") is not False
        else "unknown"
    )
    return WahaMessage(_message_id(data), chat_id, outcome)
