"""HA view contracts: auth gates and bounded streaming, synthetic HTTP only."""

import sys
from contextlib import asynccontextmanager
from types import ModuleType, SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from aiohttp import FormData, web
from aiohttp.test_utils import TestClient, TestServer

from ._loader import load_integration_module
from .test_media_core import PDF, chunks, manager, payload

# The small tests use aiohttp's real multipart/stream implementation, while HA
# owns bearer authentication before invoking a requires_auth=True view.
_stub = ModuleType("homeassistant.components.http")
_stub.HomeAssistantView = object
_stub.KEY_HASS = web.AppKey("hass-test")
_previous = sys.modules.get("homeassistant.components.http")
sys.modules["homeassistant.components.http"] = _stub
try:
    http = load_integration_module("media_http")
finally:
    if _previous is None:
        sys.modules.pop("homeassistant.components.http")
    else:
        sys.modules["homeassistant.components.http"] = _previous
media = load_integration_module("media")
backend = load_integration_module("media_backend")


async def test_registration_once_and_native_ha_auth_required():
    hass = SimpleNamespace(
        data={}, http=SimpleNamespace(register_view=lambda view: views.append(view))
    )
    views = []
    http.register_media_views(hass)
    http.register_media_views(hass)
    assert len(views) == 2
    assert all(view.requires_auth is True for view in views)
    assert {view.name for view in views} == {
        "api:waha_whatsapp:media_download",
        "api:waha_whatsapp:media_upload",
    }


@asynccontextmanager
async def http_client(tmp_path):
    transport = await manager(tmp_path)
    current_user = SimpleNamespace(id="admin", is_active=True, is_admin=True)
    transport.hass.auth = SimpleNamespace(
        async_get_user=AsyncMock(return_value=current_user)
    )
    transport.entry.domain = "waha_whatsapp"
    transport.entry.options = {"media_enabled": True}
    transport.entry.state = "loaded"
    transport.entry.runtime_data = SimpleNamespace(media_manager=transport)
    transport.hass.config_entries = SimpleNamespace(
        async_get_entry=lambda identifier: (
            transport.entry if identifier == "entry" else None
        )
    )

    @web.middleware
    async def auth_middleware(request, handler):
        # Stand-in for HA's authenticated view wrapper. Native authentication
        # itself is HA's responsibility; no test here claims a live HA check.
        if request.headers.get("Authorization") != "Bearer test-admin":
            return web.Response(status=401)
        request["hass_user"] = SimpleNamespace(
            id="admin", is_active=True, is_admin=True
        )
        return await handler(request)

    app = web.Application(middlewares=[auth_middleware])
    app[http.KEY_HASS] = transport.hass
    download, upload = http.MediaDownloadView(), http.MediaUploadView()

    async def get(request):
        return await download.get(request, **request.match_info)

    async def post(request):
        return await upload.post(request, **request.match_info)

    app.router.add_get(download.url, get)
    app.router.add_post(upload.url, post)
    async with TestClient(TestServer(app)) as client:
        yield client, transport, current_user
    await transport.async_stop()


async def test_unauthenticated_nonadmin_revoked_and_guessed_reference(tmp_path):
    async with http_client(tmp_path) as (client, transport, current_user):
        url = "/api/waha_whatsapp/entry/conversations/direct/media/att_guessed"
        response = await client.get(url)
        assert response.status == 401
        current_user.is_admin = False
        response = await client.get(url, headers={"Authorization": "Bearer test-admin"})
        assert response.status == 403
        current_user.is_admin = True
        current_user.is_active = False
        response = await client.get(url, headers={"Authorization": "Bearer test-admin"})
        assert response.status == 403
        current_user.is_active = True
        response = await client.get(url, headers={"Authorization": "Bearer test-admin"})
        assert response.status == 404
        assert (await response.json())["error"]["code"] == "media_not_found"
        assert response.headers["Cache-Control"] == "no-store"
        transport.hass.auth.async_get_user.assert_awaited()


async def test_multipart_streams_one_file_upload_never_sends_and_is_safe(tmp_path):
    async with http_client(tmp_path) as (client, transport, _):
        data = FormData()
        data.add_field("file", PDF, filename="test.pdf", content_type="application/pdf")
        response = await client.post(
            "/api/waha_whatsapp/entry/conversations/direct/media",
            data=data,
            headers={"Authorization": "Bearer test-admin"},
        )
        assert response.status == 201
        receipt = await response.json()
        assert receipt["size_bytes"] == len(PDF)
        assert receipt["kind"] == "file"
        assert receipt["upload_id"].startswith("upload_")
        assert "file" not in receipt
        assert "path" not in receipt
        upload = transport.get_upload(receipt["upload_id"], "direct", "admin")
        assert upload.sha256 == receipt["sha256"]
        assert len(transport.store.records) == 1


@pytest.mark.parametrize(
    "invalid", ["extra_file", "wrong_name", "dishonest_type", "not_multipart"]
)
async def test_invalid_multipart_never_leaves_staging_file(tmp_path, invalid):
    async with http_client(tmp_path) as (client, transport, _):
        data = FormData()
        if invalid == "not_multipart":
            data = b"not multipart"
        else:
            data.add_field(
                "wrong" if invalid == "wrong_name" else "file",
                PDF,
                filename="x",
                content_type="image/png"
                if invalid == "dishonest_type"
                else "application/pdf",
            )
            if invalid == "extra_file":
                data.add_field(
                    "file", PDF, filename="second", content_type="application/pdf"
                )
        response = await client.post(
            "/api/waha_whatsapp/entry/conversations/direct/media",
            data=data,
            headers={"Authorization": "Bearer test-admin"},
        )
        assert response.status in (400, 415)
        assert not list(transport.store.path.glob("*.bin"))
        assert transport.store.reservations == 0
        assert not transport.store.records


async def test_binary_download_headers_bytes_and_reauthorization(tmp_path, monkeypatch):
    async with http_client(tmp_path) as (client, transport, _):
        attachment = (
            await transport.async_observe(
                payload(info="document", mime="application/pdf", document=True),
                "msg",
                "direct",
            )
        )[0]

        @asynccontextmanager
        async def download(*args):
            yield {"mime_type": "application/pdf"}, chunks()

        monkeypatch.setattr(backend, "async_download", download)
        result = await transport.async_get_media(
            "direct", "msg", attachment["attachment_id"]
        )
        url = result["download"]["path"]
        response = await client.get(url, headers={"Authorization": "Bearer test-admin"})
        assert response.status == 200
        assert await response.read() == PDF
        assert response.headers["Content-Length"] == str(len(PDF))
        assert response.headers["Content-Type"] == "application/pdf"
        assert response.headers["X-Content-Type-Options"] == "nosniff"
        assert response.headers["Cache-Control"] == "no-store"
        assert response.headers["Content-Disposition"].startswith("attachment")
        response = await client.get(
            url, headers={"Authorization": "Bearer test-admin", "Range": "bytes=0-1"}
        )
        assert response.status == 400
        transport.authorize.return_value = "rebound"
        response = await client.get(url, headers={"Authorization": "Bearer test-admin"})
        assert response.status == 404
        assert b"PDF" not in await response.read()


async def _ready_download(transport, monkeypatch):
    attachment = (
        await transport.async_observe(
            payload(info="document", mime="application/pdf", document=True),
            "msg",
            "direct",
        )
    )[0]

    @asynccontextmanager
    async def download(*args):
        yield {"mime_type": "application/pdf"}, chunks()

    monkeypatch.setattr(backend, "async_download", download)
    result = await transport.async_get_media(
        "direct", "msg", attachment["attachment_id"]
    )
    return attachment["attachment_id"], result["download"]["path"]


@pytest.mark.parametrize("blocked_operation", ["open_stream", "read"])
@pytest.mark.parametrize(
    ("change", "status"),
    [
        ("admin", 403),
        ("manager", 503),
        ("disable", 503),
        ("unload", 503),
        ("route", 404),
        ("expiry", 410),
    ],
)
async def test_changes_while_file_open_or_read_blocked_release_no_bytes(
    tmp_path, monkeypatch, blocked_operation, change, status
):
    import asyncio
    from time import time

    async with http_client(tmp_path) as (client, transport, current_user):
        attachment_id, url = await _ready_download(transport, monkeypatch)
        entered, release = asyncio.Event(), asyncio.Event()
        original_io = http._io
        blocked = False

        async def blocking_io(hass, function, *args):
            nonlocal blocked
            result = await original_io(hass, function, *args)
            if not blocked and function.__name__ == blocked_operation:
                blocked = True
                entered.set()
                await release.wait()
            return result

        monkeypatch.setattr(http, "_io", blocking_io)
        request = asyncio.ensure_future(
            client.get(url, headers={"Authorization": "Bearer test-admin"})
        )
        try:
            await asyncio.wait_for(entered.wait(), 2)
            if change == "admin":
                current_user.is_admin = False
            elif change == "manager":
                transport.entry.runtime_data.media_manager = SimpleNamespace(
                    enabled=True
                )
            elif change == "disable":
                transport.entry.options["media_enabled"] = False
            elif change == "unload":
                transport.entry.state = "not_loaded"
            elif change == "route":
                transport.authorize.return_value = "rebound"
                transport.observe_binding = lambda conversation, **kwargs: "rebound"
            else:
                transport.store.records[attachment_id]["expires_at"] = time() - 1
        finally:
            release.set()
        response = await request
        assert response.status == status
        body = await response.read()
        assert b"%PDF" not in body
        assert response.headers["Content-Type"].startswith("application/json")
        assert not transport.store.leases


async def test_opened_descriptor_is_rechecked_for_digest_and_size(
    tmp_path, monkeypatch
):
    async with http_client(tmp_path) as (client, transport, _):
        attachment_id, url = await _ready_download(transport, monkeypatch)
        original_io = http._io

        async def replaced_after_open(hass, function, *args):
            result = await original_io(hass, function, *args)
            if function.__name__ == "open_stream":
                record = transport.store.records[attachment_id]
                path = transport.store.path / record["file"]
                path.write_bytes(b"X" * len(PDF))
            return result

        monkeypatch.setattr(http, "_io", replaced_after_open)
        response = await client.get(url, headers={"Authorization": "Bearer test-admin"})
        assert response.status == 422
        assert (await response.json())["error"]["code"] == "invalid_media"


async def test_principal_revoked_while_multipart_read_blocked_cannot_commit(
    tmp_path, monkeypatch
):
    import asyncio

    from aiohttp.multipart import BodyPartReader

    async with http_client(tmp_path) as (client, transport, current_user):
        entered, release = asyncio.Event(), asyncio.Event()
        read_chunk = BodyPartReader.read_chunk
        blocked = False

        async def blocked_read(part, size):
            nonlocal blocked
            if not blocked:
                blocked = True
                entered.set()
                await release.wait()
            return await read_chunk(part, size)

        monkeypatch.setattr(BodyPartReader, "read_chunk", blocked_read)
        data = FormData()
        data.add_field("file", PDF, filename="test.pdf", content_type="application/pdf")
        request = asyncio.ensure_future(
            client.post(
                "/api/waha_whatsapp/entry/conversations/direct/media",
                data=data,
                headers={"Authorization": "Bearer test-admin"},
            )
        )
        try:
            await asyncio.wait_for(entered.wait(), 2)
            current_user.is_admin = False
        finally:
            release.set()
        response = await request
        assert response.status == 403
        assert (await response.json())["error"]["code"] == "not_authorized"
        assert not transport.store.records
        assert not list(transport.store.path.glob("*.bin"))
        assert transport.store.reservations == 0


def _direct_request(transport, *, headers=None):
    transport.entry.domain = "waha_whatsapp"
    transport.entry.options = {"media_enabled": True}
    transport.entry.state = "loaded"
    transport.entry.runtime_data = SimpleNamespace(media_manager=transport)
    user = SimpleNamespace(id="admin", is_active=True, is_admin=True)
    transport.hass.auth = SimpleNamespace(async_get_user=AsyncMock(return_value=user))
    transport.hass.config_entries = SimpleNamespace(
        async_get_entry=lambda _id: transport.entry
    )

    class Request(dict):
        transport = None
        content_length = None
        content_type = "multipart/form-data"

        async def multipart(self):
            raise AssertionError("invalid encoding must be rejected before multipart")

    request = Request(hass_user=user)
    request.app = {http.KEY_HASS: transport.hass}
    request.headers = headers or {}
    return request


async def test_repeated_cancel_during_http_close_releases_descriptor_and_lease(
    tmp_path, monkeypatch
):
    import asyncio

    transport = await manager(tmp_path)
    request = _direct_request(transport)
    attachment_id, _ = await _ready_download(transport, monkeypatch)
    reading, closing, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
    executor = transport.hass.async_add_executor_job
    closed = []

    async def stalled_close(function, *args):
        if function.__name__ == "close":
            closed.append(function.__self__)
            closing.set()
            await release.wait()
        return await executor(function, *args)

    transport.hass.async_add_executor_job = stalled_close
    original_io = http._io

    async def blocked_read(hass, function, *args):
        if function.__name__ == "read":
            reading.set()
            await asyncio.Event().wait()
        return await original_io(hass, function, *args)

    monkeypatch.setattr(http, "_io", blocked_read)
    task = asyncio.create_task(
        http.MediaDownloadView().get(request, "entry", "direct", attachment_id)
    )
    await asyncio.wait_for(reading.wait(), 2)
    task.cancel()
    await asyncio.wait_for(closing.wait(), 2)
    task.cancel()
    await asyncio.sleep(0)
    task.cancel()
    await asyncio.sleep(0)
    assert not task.done()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert closed
    assert all(stream.closed for stream in closed)
    assert not transport.store.leases
    assert not transport._operations
    assert transport.store.reservations == 0
    assert len(list(transport.store.path.glob("*.bin"))) == 1


async def test_upload_rejects_top_level_compression_before_multipart(tmp_path):
    transport = await manager(tmp_path)
    request = _direct_request(transport, headers={"Content-Encoding": "gzip"})
    response = await http.MediaUploadView().post(request, "entry", "direct")
    assert response.status == 400
    assert not transport.store.records
    assert not list(transport.store.path.glob("*.bin"))
