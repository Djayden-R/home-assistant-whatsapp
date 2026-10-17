"""Synthetic contract checks for the private WAHA media submission path."""

import asyncio
import base64
import hashlib
import json
import threading
from types import SimpleNamespace

import pytest

from ._loader import load_integration_module
from .test_api import FakeSession, make_client

backend = load_integration_module("media_backend_send")
api = load_integration_module("api")


class Response:
    def __init__(self, status=200, body=b'{"id":"provider-id"}'):
        self.status, self.body = status, body
        self.content = self
        self.headers = {}

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        pass

    async def iter_chunked(self, _size):
        for start in range(0, len(self.body), 7):
            yield self.body[start : start + 7]


def staged(tmp_path, *, kind="file", mime="application/pdf", opus=False):
    data = b"%PDF-1.7\nSynthetic generated transport fixture\n%%EOF\n"
    path = tmp_path / "private.bin"
    path.write_bytes(data)
    receipt = SimpleNamespace(
        kind=kind,
        mime_type=mime,
        native_opus=opus,
        file_name="fixture.pdf",
        size_bytes=len(data),
        sha256=hashlib.sha256(data).hexdigest(),
    )
    return path, receipt, data


@pytest.mark.parametrize(
    ("kind", "mime", "voice", "endpoint"),
    [
        ("file", "application/pdf", False, "sendFile"),
        ("image", "image/png", False, "sendImage"),
        ("audio", "audio/mpeg", False, "sendFile"),
        ("audio", "audio/ogg", True, "sendVoice"),
    ],
)
async def test_exact_private_endpoint_payload_and_bounded_timeout(
    tmp_path,
    kind,
    mime,
    voice,
    endpoint,
):
    path, receipt, data = staged(tmp_path, kind=kind, mime=mime, opus=voice)
    session = FakeSession(Response())
    result = await backend.async_send_staged_media(
        make_client(session),
        path,
        receipt,
        "123456@c.us",
        kind=kind,
        as_voice_note=voice,
        caption=None if voice else "Caption",
        reply_to_message_id="raw-quote",
    )
    assert result.outcome == "accepted"
    call = session.requests[0]
    assert call["method"] == "POST"
    assert call["url"] == f"http://waha.internal:3000/api/{endpoint}"
    assert call["headers"]["X-Api-Key"] == "secret-api-key"
    assert call["allow_redirects"] is False
    assert call["timeout"].connect == 10
    assert call["timeout"].total == 60
    payload = call["json"]
    assert payload["session"] == "house"
    assert payload["chatId"] == "123456@c.us"
    assert payload["reply_to"] == "raw-quote"
    assert base64.b64decode(payload["file"]["data"]) == data
    assert "url" not in payload["file"]
    if voice:
        assert payload["convert"] is False
        assert payload["file"]["mimetype"] == "audio/ogg; codecs=opus"
        assert "caption" not in payload


@pytest.mark.parametrize("chat", ["../private", "https://evil.test", "123@newsletter"])
async def test_arbitrary_destination_never_reaches_network(tmp_path, chat):
    path, receipt, _data = staged(tmp_path)
    session = FakeSession(Response())
    with pytest.raises(ValueError, match="invalid_conversation"):
        await backend.async_send_staged_media(
            make_client(session), path, receipt, chat, kind="file"
        )
    assert not session.requests


async def test_integrity_change_never_reaches_network(tmp_path):
    path, receipt, _data = staged(tmp_path)
    path.write_bytes(b"changed")
    session = FakeSession(Response())
    with pytest.raises(ValueError, match="invalid_media"):
        await backend.async_send_staged_media(
            make_client(session), path, receipt, "123@c.us", kind="file"
        )
    assert not session.requests


@pytest.mark.parametrize(
    ("status", "error"),
    [
        (401, api.WahaAuthenticationError),
        (429, api.WahaRequestError),
        (500, api.WahaRequestError),
        (302, api.WahaResponseError),
    ],
)
async def test_rejection_and_redirect_do_not_retry(tmp_path, status, error):
    path, receipt, _data = staged(tmp_path)
    session = FakeSession(Response(status))
    with pytest.raises(error):
        await backend.async_send_staged_media(
            make_client(session), path, receipt, "123@c.us", kind="file"
        )
    assert len(session.requests) == 1


@pytest.mark.parametrize("body", [b"not json", b"x" * 65537])
async def test_malformed_or_large_success_is_uncertain(tmp_path, body):
    path, receipt, _data = staged(tmp_path)
    with pytest.raises(api.WahaResponseError):
        await backend.async_send_staged_media(
            make_client(FakeSession(Response(body=body))),
            path,
            receipt,
            "123@c.us",
            kind="file",
        )


async def test_timeout_does_not_retry(tmp_path):
    path, receipt, _data = staged(tmp_path)
    session = SimpleNamespace(
        request=lambda *_a, **_kw: (_ for _ in ()).throw(TimeoutError())
    )
    with pytest.raises(api.WahaConnectionError):
        await backend.async_send_staged_media(
            make_client(session), path, receipt, "123@c.us", kind="file"
        )


async def test_native_voice_requires_verified_opus_and_rejects_caption(tmp_path):
    path, receipt, _data = staged(tmp_path, kind="audio", mime="audio/ogg")
    session = FakeSession(Response())
    with pytest.raises(ValueError, match="unsupported"):
        await backend.async_send_staged_media(
            make_client(session),
            path,
            receipt,
            "123@c.us",
            kind="audio",
            as_voice_note=True,
        )
    receipt.native_opus = True
    with pytest.raises(ValueError, match="unsupported"):
        await backend.async_send_staged_media(
            make_client(session),
            path,
            receipt,
            "123@c.us",
            kind="audio",
            as_voice_note=True,
            caption="Do not drop me",
        )
    assert not session.requests


async def test_explicit_false_success_preserves_unknown(tmp_path):
    path, receipt, _data = staged(tmp_path)
    result = await backend.async_send_staged_media(
        make_client(
            FakeSession(Response(body=json.dumps({"success": False}).encode()))
        ),
        path,
        receipt,
        "123@g.us",
        kind="file",
    )
    assert result.outcome == "unknown"


async def test_encoded_backend_response_never_bypasses_response_bound(tmp_path):
    path, receipt, _ = staged(tmp_path)
    response = Response()
    response.headers["Content-Encoding"] = "gzip"
    session = FakeSession(response)
    client = make_client(session)
    with pytest.raises(api.WahaResponseError):
        await backend.async_send_staged_media(
            client, path, receipt, "123@c.us", kind="file"
        )
    assert session.requests[0]["auto_decompress"] is False
    assert session.requests[0]["headers"]["Accept-Encoding"] == "identity"


async def test_cancelled_preparation_drains_physical_read_before_lease_release(
    tmp_path,
    monkeypatch,
):
    path, receipt, _data = staged(tmp_path)
    entered, release = threading.Event(), threading.Event()

    def prepare(*_args, **_kwargs):
        entered.set()
        release.wait(timeout=3)
        return {}

    monkeypatch.setattr(backend, "_payload", prepare)
    session = FakeSession(Response())
    task = asyncio.create_task(
        backend.async_send_staged_media(
            make_client(session),
            path,
            receipt,
            "123@c.us",
            kind="file",
        )
    )
    assert await asyncio.to_thread(entered.wait, 1)
    task.cancel()
    await asyncio.sleep(0)
    assert not task.done()
    assert not session.requests
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not session.requests


async def test_symlink_swap_is_failed_before_network(tmp_path):
    path, receipt, _data = staged(tmp_path)
    other = tmp_path / "outside.bin"
    path.rename(other)
    path.symlink_to(other)
    session = FakeSession(Response())
    with pytest.raises(ValueError, match="invalid_media"):
        await backend.async_send_staged_media(
            make_client(session),
            path,
            receipt,
            "123@c.us",
            kind="file",
        )
    assert not session.requests
