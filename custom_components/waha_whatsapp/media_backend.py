"""Exact-message GOWS retrieval and credentialed, same-origin media streaming."""

from __future__ import annotations

import asyncio
import json
import re
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any
from urllib.parse import quote, unquote, urljoin, urlsplit

from aiohttp import ClientError, ClientTimeout

from .media import CHUNK_BYTES, MAX_BYTES, MediaError, normalized_mime

MAX_JSON_BYTES = 1024 * 1024
TIMEOUT = ClientTimeout(total=60, connect=10, sock_connect=10)


def safe_file_url(base_url: str, value: object) -> str:
    """Accept only the configured origin's ordinary authenticated files route."""
    if not isinstance(value, str) or len(value) > 2048 or "\\" in value:
        raise MediaError("invalid_media", http_status=502)
    try:
        original_path = unquote(unquote(urlsplit(value).path))
        if any(part in (".", "..") for part in original_path.split("/")):
            raise MediaError("invalid_media", http_status=502)
        url = urljoin(base_url.rstrip("/") + "/", value)
    except ValueError as err:
        raise MediaError("invalid_media", http_status=502) from err
    base, target = urlsplit(base_url), urlsplit(url)
    try:
        same_origin = (target.scheme, target.hostname, target.port) == (
            base.scheme,
            base.hostname,
            base.port,
        )
    except ValueError as err:
        raise MediaError("invalid_media", http_status=502) from err
    decoded = unquote(unquote(target.path))
    if (
        not same_origin
        or target.scheme not in ("http", "https")
        or target.username
        or target.password
        or target.query
        or target.fragment
        or not decoded.startswith("/api/files/")
        or not all(
            re.fullmatch(r"[A-Za-z0-9_@.-]+", part) and part not in (".", "..")
            for part in decoded.removeprefix("/api/files/").split("/")
        )
    ):
        raise MediaError("invalid_media", http_status=502)
    return url


def _status(status: int) -> None:
    if status in (404, 410):
        raise MediaError("media_not_found", http_status=404)
    if status == 429:
        raise MediaError("rate_limited", retryable=True, http_status=429)
    if status != 200:
        raise MediaError(
            "channel_unavailable" if status in (401, 403) else "transport_failure",
            retryable=True,
            http_status=502,
        )


def _identity_encoding(response: Any) -> None:
    encoding = response.headers.get("Content-Encoding", "identity").strip().lower()
    if encoding not in ("", "identity"):
        raise MediaError("invalid_media", http_status=502)


@asynccontextmanager
async def async_download(
    client: Any, record: dict[str, Any]
) -> AsyncIterator[tuple[dict[str, Any], AsyncIterator[bytes]]]:
    """Retrieve exactly one trusted binding; never ingest history or emit events."""
    headers = {
        "X-Api-Key": client._api_key,
        "Accept": "application/json",
        "Accept-Encoding": "identity",
    }
    path = "/api/{}/chats/{}/messages/{}".format(
        *(
            quote(str(value), safe="")
            for value in (
                client.session_name,
                record["raw_chat_id"],
                record["raw_message_id"],
            )
        )
    )
    try:
        async with asyncio.timeout(60):
            async with client._session.request(
                "GET",
                client.base_url.rstrip("/") + path,
                headers=headers,
                params={"downloadMedia": "true"},
                allow_redirects=False,
                auto_decompress=False,
                timeout=TIMEOUT,
            ) as response:
                _status(response.status)
                _identity_encoding(response)
                if (
                    response.content_length is not None
                    and response.content_length > MAX_JSON_BYTES
                ):
                    raise MediaError("invalid_media", http_status=502)
                body = bytearray()
                async for chunk in response.content.iter_chunked(CHUNK_BYTES):
                    body.extend(chunk)
                    if len(body) > MAX_JSON_BYTES:
                        raise MediaError("invalid_media", http_status=502)
                try:
                    message = json.loads(body)
                except (ValueError, UnicodeDecodeError) as err:
                    raise MediaError("invalid_media", http_status=502) from err
            if (
                not isinstance(message, dict)
                or message.get("id") != record["raw_message_id"]
                or message.get("session", client.session_name) != client.session_name
                or message.get("chatId", message.get("from")) != record["raw_chat_id"]
                or message.get("fromMe") is not False
            ):
                raise MediaError("invalid_media", http_status=502)
            if message.get("from") != record.get("raw_from", record["raw_chat_id"]):
                raise MediaError("invalid_media", http_status=502)
            original_participant = record.get("raw_participant")
            if (
                original_participant is not None
                and message.get(
                    "participant",
                    message.get("to")
                    if record["raw_chat_id"].endswith("@g.us")
                    else None,
                )
                != original_participant
            ):
                raise MediaError("invalid_media", http_status=502)
            media = message.get("media")
            if message.get("hasMedia") is not True or not isinstance(media, dict):
                raise MediaError("media_not_found", http_status=404)
            mime = normalized_mime(media.get("mimetype"))
            if not mime:
                raise MediaError("unsupported_media_type", http_status=415)
            url = safe_file_url(client.base_url, media.get("url"))
            async with client._session.request(
                "GET",
                url,
                headers={
                    "X-Api-Key": client._api_key,
                    "Accept": "application/octet-stream",
                    "Accept-Encoding": "identity",
                },
                allow_redirects=False,
                auto_decompress=False,
                timeout=TIMEOUT,
            ) as response:
                _status(response.status)
                _identity_encoding(response)
                if (
                    response.content_length is not None
                    and response.content_length > MAX_BYTES
                ):
                    raise MediaError("media_too_large", http_status=413)
                yield {"mime_type": mime}, response.content.iter_chunked(CHUNK_BYTES)
    except TimeoutError as err:
        raise MediaError("transport_failure", retryable=True, http_status=504) from err
    except (ClientError, OSError) as err:
        raise MediaError("transport_failure", retryable=True, http_status=502) from err
