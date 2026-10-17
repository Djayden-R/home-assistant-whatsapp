"""HA-authenticated, administrator-only bounded media upload and download."""

from __future__ import annotations

import asyncio
import os
from typing import Any
from urllib.parse import quote

from aiohttp import web
from homeassistant.components.http import KEY_HASS, HomeAssistantView

from .const import DOMAIN
from .media import CHUNK_BYTES, MAX_BYTES, MediaError, _finish_cleanup, _io

_BASE = "/api/waha_whatsapp/{config_entry_id}/conversations/{conversation_id}/media"


async def _manager(request: web.Request, entry_id: str) -> Any:
    user = request.get("hass_user")
    hass = request.app[KEY_HASS]
    user = await hass.auth.async_get_user(user.id) if user is not None else None
    if user is None or not user.is_active or not user.is_admin:
        raise MediaError("not_authorized", http_status=403)
    entry = hass.config_entries.async_get_entry(entry_id)
    if entry is None or entry.domain != DOMAIN:
        raise MediaError("media_not_found", http_status=404)
    if entry.options.get("media_enabled") is not True:
        raise MediaError("media_disabled")
    if getattr(entry.state, "value", entry.state) != "loaded":
        raise MediaError("channel_unavailable")
    runtime = getattr(entry, "runtime_data", None)
    manager = getattr(runtime, "media_manager", None)
    if manager is None or not manager.enabled:
        raise MediaError("media_disabled", http_status=503)
    return manager


async def _same_manager(request: web.Request, entry_id: str, expected: Any) -> None:
    if await _manager(request, entry_id) is not expected:
        raise MediaError("channel_unavailable")


def _error(error: MediaError) -> web.Response:
    headers = {"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"}
    if error.http_status == 429:
        headers["Retry-After"] = "5"
    return web.json_response(error.as_dict(), status=error.http_status, headers=headers)


class MediaDownloadView(HomeAssistantView):
    url = _BASE + "/{attachment_id}"
    name = "api:waha_whatsapp:media_download"
    requires_auth = True

    async def get(
        self,
        request: web.Request,
        config_entry_id: str,
        conversation_id: str,
        attachment_id: str,
    ) -> web.StreamResponse:
        try:
            manager = await _manager(request, config_entry_id)
            if "Range" in request.headers:
                raise MediaError("invalid_request", http_status=400)
            async with manager.async_download(conversation_id, attachment_id) as (
                metadata,
                path,
            ):
                headers = {
                    "Content-Type": metadata["mime_type"],
                    "Content-Length": str(metadata["size_bytes"]),
                    "Cache-Control": "no-store",
                    "X-Content-Type-Options": "nosniff",
                    "Content-Disposition": "attachment",
                }
                if metadata["file_name"]:
                    headers["Content-Disposition"] += "; filename*=UTF-8''" + quote(
                        metadata["file_name"], safe=""
                    )
                response = web.StreamResponse(status=200, headers=headers)
                stream = None

                def open_stream():
                    nonlocal stream
                    stream = os.fdopen(os.open(path, os.O_RDONLY | os.O_NOFOLLOW), "rb")

                try:
                    await _io(manager.hass, open_stream)
                    await manager.async_validate_download(
                        conversation_id, attachment_id, stream
                    )
                    await _same_manager(request, config_entry_id, manager)
                    async with asyncio.timeout(60):
                        remaining = metadata["size_bytes"]
                        while remaining:
                            chunk = await _io(
                                manager.hass, stream.read, min(CHUNK_BYTES, remaining)
                            )
                            if not chunk:
                                raise MediaError("invalid_media")
                            manager.validate_download_reference(
                                conversation_id, attachment_id
                            )
                            await _same_manager(request, config_entry_id, manager)
                            if not response.prepared:
                                await response.prepare(request)
                            await response.write(chunk)
                            remaining -= len(chunk)
                        await response.write_eof()
                except MediaError, TimeoutError, OSError, ConnectionError:
                    if response.prepared:
                        # Once headers/bytes were sent, close instead of emitting a
                        # second JSON response or releasing any further file bytes.
                        response.force_close()
                        if request.transport is not None:
                            request.transport.close()
                        return response
                    raise
                finally:
                    if stream:
                        await _finish_cleanup(_io(manager.hass, stream.close))
                return response
        except MediaError as err:
            return _error(err)
        except TimeoutError:
            return _error(
                MediaError("transport_failure", retryable=True, http_status=504)
            )
        except OSError:
            return _error(MediaError("media_not_found"))


class MediaUploadView(HomeAssistantView):
    url = _BASE
    name = "api:waha_whatsapp:media_upload"
    requires_auth = True

    async def post(
        self, request: web.Request, config_entry_id: str, conversation_id: str
    ) -> web.Response:
        try:
            manager = await _manager(request, config_entry_id)
            if request.content_length and request.content_length > MAX_BYTES + 65536:
                raise MediaError("media_too_large", http_status=413)
            if request.headers.get(
                "Content-Encoding", "identity"
            ).strip().lower() not in ("", "identity"):
                raise MediaError("invalid_request")
            if request.content_type != "multipart/form-data":
                raise MediaError("invalid_request")
            async with asyncio.timeout(60):
                reader = await request.multipart()
                part = await reader.next()
                if (
                    part is None
                    or part.name != "file"
                    or part.headers.get("Content-Transfer-Encoding")
                    or part.headers.get("Content-Encoding")
                ):
                    raise MediaError("invalid_request")

                async def chunks():
                    while chunk := await part.read_chunk(CHUNK_BYTES):
                        yield bytes(chunk)
                    # Do not auto-drain unbounded extra fields/attachments.
                    if await reader.next() is not None:
                        raise MediaError("invalid_request")

                async def authorize_principal(principal_id):
                    await _same_manager(request, config_entry_id, manager)
                    return principal_id == request["hass_user"].id

                receipt = await manager.async_stage(
                    conversation_id,
                    request["hass_user"].id,
                    chunks(),
                    part.headers.get("Content-Type"),
                    part.filename,
                    authorize_principal=authorize_principal,
                )
                await _same_manager(request, config_entry_id, manager)
                return web.json_response(
                    receipt.as_dict(),
                    status=201,
                    headers={
                        "Cache-Control": "no-store",
                        "X-Content-Type-Options": "nosniff",
                    },
                )
        except MediaError as err:
            return _error(err)
        except TimeoutError:
            return _error(
                MediaError("transport_failure", retryable=True, http_status=504)
            )
        except ValueError, AssertionError:
            return _error(MediaError("invalid_request"))


def register_media_views(hass: Any) -> None:
    """Register once; loaded-entry lookup and authorization happen per request."""
    marker = "waha_whatsapp_media_http_registered"
    if not hass.data.get(marker):
        hass.http.register_view(MediaDownloadView())
        hass.http.register_view(MediaUploadView())
        hass.data[marker] = True
