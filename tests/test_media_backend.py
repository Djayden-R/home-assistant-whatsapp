"""Exact-message backend security contract with synthetic streaming responses."""

import json
from types import SimpleNamespace

import pytest

from ._loader import load_integration_module
from .test_media_core import PDF

backend = load_integration_module("media_backend")
media = load_integration_module("media")


class Content:
    def __init__(self, content):
        self.content = content
        self.reads = 0

    async def iter_chunked(self, count):
        self.reads += 1
        for start in range(0, len(self.content), count):
            yield self.content[start : start + count]


class Response:
    def __init__(self, body, status=200, content_length=None, headers=None):
        self.status = status
        self.content = Content(body)
        self.content_length = content_length
        self.headers = headers or {}

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        pass


class Session:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []

    def request(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        return self.responses.pop(0)


def binding():
    return {
        "raw_message_id": "false_111@c.us_TEST",
        "raw_chat_id": "111@c.us",
        "raw_from": "111@c.us",
        "raw_participant": None,
    }


def message(**overrides):
    return {
        "id": "false_111@c.us_TEST",
        "from": "111@c.us",
        "fromMe": False,
        "hasMedia": True,
        "media": {
            "mimetype": "application/pdf",
            "url": "http://waha.invalid:3000/api/files/random.pdf",
        },
        **overrides,
    }


def client(*responses):
    return SimpleNamespace(
        _session=Session(*responses),
        _api_key="test-secret",
        session_name="house",
        base_url="http://waha.invalid:3000",
    )


@pytest.mark.parametrize(
    "url",
    [
        "http://evil.invalid/api/files/random.pdf",
        "https://waha.invalid:3000/api/files/random.pdf",
        "http://user:secret@waha.invalid:3000/api/files/random.pdf",
        "/api/files/a/../secret.pdf",
        "/api/files/%2e%2e/sessions",
        "/api/files/%252e%252e/sessions",
        "/api/files/x?token=secret",
        "/api/files/x#secret",
        "/etc/passwd",
        "file:///api/files/x",
        "/api/files/a\\b",
        "/api/files//secret",
    ],
)
def test_only_exact_origin_files_without_traversal_or_credentials(url):
    with pytest.raises(media.MediaError, match="invalid_media"):
        backend.safe_file_url("http://waha.invalid:3000", url)


def test_same_origin_api_files_path_is_accepted():
    assert backend.safe_file_url(
        "http://waha.invalid:3000", "/api/files/false_111@c.us_SYNTHETIC.pdf"
    ).endswith("false_111@c.us_SYNTHETIC.pdf")
    assert (
        backend.safe_file_url("http://waha.invalid:3000", "/api/files/house/random.pdf")
        == "http://waha.invalid:3000/api/files/house/random.pdf"
    )


async def test_download_is_exact_on_demand_with_credentials_and_no_redirects():
    result = client(Response(json.dumps(message()).encode()), Response(PDF))
    async with backend.async_download(result, binding()) as (metadata, chunks):
        content = b"".join([part async for part in chunks])
    assert content == PDF
    assert metadata == {"mime_type": "application/pdf"}
    calls = result._session.calls
    assert len(calls) == 2
    assert calls[0][0][1].endswith(
        "/api/house/chats/111%40c.us/messages/false_111%40c.us_TEST"
    )
    assert calls[0][1]["params"] == {"downloadMedia": "true"}
    assert all(call[1]["allow_redirects"] is False for call in calls)
    assert all(call[1]["headers"]["X-Api-Key"] == "test-secret" for call in calls)
    assert all(call[1]["timeout"].connect == 10 for call in calls)


@pytest.mark.parametrize(
    "overrides",
    [
        {"id": "wrong"},
        {"chatId": "other@c.us"},
        {"session": "other"},
        {"from": "other@c.us", "chatId": "111@c.us"},
        {"fromMe": True},
    ],
)
async def test_identity_mismatch_never_fetches_file(overrides):
    result = client(Response(json.dumps(message(**overrides)).encode()))
    with pytest.raises(media.MediaError, match="invalid_media"):
        async with backend.async_download(result, binding()):
            pytest.fail("identity mismatch accepted")
    assert len(result._session.calls) == 1


async def test_group_original_participant_is_exactly_bound():
    expected = binding() | {
        "raw_chat_id": "123@g.us",
        "raw_from": "123@g.us",
        "raw_participant": "111@lid",
    }
    data = message(
        chatId="123@g.us", **{"from": "123@g.us", "participant": "other@lid"}
    )
    result = client(Response(json.dumps(data).encode()))
    with pytest.raises(media.MediaError, match="invalid_media"):
        async with backend.async_download(result, expected):
            pytest.fail("wrong group speaker accepted")


@pytest.mark.parametrize(
    ("response", "code"),
    [
        (Response(b"{}", status=302), "transport_failure"),
        (Response(b"{}", status=404), "media_not_found"),
        (Response(b"{}", status=429), "rate_limited"),
        (Response(b"{}", status=401), "channel_unavailable"),
        (Response(b"{"), "invalid_media"),
        (Response(b"{}", content_length=backend.MAX_JSON_BYTES + 1), "invalid_media"),
        (Response(b" " * (backend.MAX_JSON_BYTES + 1)), "invalid_media"),
    ],
)
async def test_backend_failures_are_bounded_safe_codes(response, code):
    result = client(response)
    with pytest.raises(media.MediaError, match=code) as error:
        async with backend.async_download(result, binding()):
            pytest.fail("invalid response accepted")
    assert "test-secret" not in str(error.value)


async def test_file_redirect_or_oversize_fails_without_third_request():
    for response, code in (
        (Response(b"", status=302), "transport_failure"),
        (Response(b"", content_length=media.MAX_BYTES + 1), "media_too_large"),
    ):
        result = client(Response(json.dumps(message()).encode()), response)
        with pytest.raises(media.MediaError, match=code):
            async with backend.async_download(result, binding()):
                pytest.fail("unsafe file accepted")
        assert len(result._session.calls) == 2


@pytest.mark.parametrize("which", ["message", "file"])
async def test_compressed_responses_are_rejected_before_body_is_read(which):
    compressed = Response(b"gzip-marker", headers={"Content-Encoding": "gzip"})
    responses = (
        [compressed]
        if which == "message"
        else [Response(json.dumps(message()).encode()), compressed]
    )
    result = client(*responses)
    with pytest.raises(media.MediaError, match="invalid_media"):
        async with backend.async_download(result, binding()):
            pytest.fail("compressed backend response accepted")
    assert compressed.content.reads == 0
    assert all(call[1]["auto_decompress"] is False for call in result._session.calls)
    assert all(
        call[1]["headers"]["Accept-Encoding"] == "identity"
        for call in result._session.calls
    )
