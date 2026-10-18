"""Media requests share the durable text namespace and preserve uncertainty."""

import asyncio
import hashlib
from contextlib import asynccontextmanager
from copy import deepcopy
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from ._loader import load_integration_module
from .test_correlated import FakeStore, manager

media = load_integration_module("media")
send = load_integration_module("media_send")
api = load_integration_module("api")
outbound = load_integration_module("outbound")
correlated = load_integration_module("correlated")

DATA = {
    "config_entry_id": "entry-a",
    "conversation_id": "direct_a",
    "upload_id": "upload-a",
    "kind": "file",
    "client_request_id": "request-a",
    "origin": "application",
    "caption": "Synthetic caption",
}


class Staging:
    enabled = True

    def __init__(self, tmp_path):
        self.path = tmp_path / "private.bin"
        self.path.write_bytes(b"%PDF-1.7\nGenerated\n%%EOF")
        self.receipt = media.UploadReceipt(
            "upload-a",
            "entry-a",
            "direct_a",
            "admin-a",
            "file",
            "application/pdf",
            "generated.pdf",
            self.path.stat().st_size,
            hashlib.sha256(self.path.read_bytes()).hexdigest(),
            9999999999,
            "route-binding",
        )
        self.receipts = {"upload-a": self.receipt}
        self.opens = 0
        self.expired = False

    def get_upload(self, upload_id, conversation_id, principal_id, **_kw):
        receipt = self.receipts.get(upload_id)
        if (
            receipt is None
            or receipt.conversation_id != conversation_id
            or receipt.principal_id != principal_id
        ):
            raise media.MediaError("media_not_found")
        return receipt

    @asynccontextmanager
    async def async_open_upload(self, upload_id, conversation_id, principal_id):
        self.opens += 1
        if self.expired:
            raise media.MediaError("media_expired")
        yield self.get_upload(upload_id, conversation_id, principal_id), self.path


async def setup(tmp_path, monkeypatch, *, store=None, hass=None):
    ledger = (
        manager(store)
        if hass is None
        else correlated.CorrelatedSendManager(
            hass, SimpleNamespace(entry_id="entry-a"), store=store
        )
    )
    await ledger.async_start()
    staging = Staging(tmp_path)
    registry = SimpleNamespace(
        resolve_message=lambda opaque, _conv: (
            "raw-id" if opaque == "msg_safe" else None
        ),
        message_token=lambda _raw: "msg_safe",
        async_remember_message=AsyncMock(return_value="msg_safe"),
    )
    events = []
    entry = SimpleNamespace(
        entry_id="entry-a", options={"outbound_observations_enabled": True}
    )
    runtime = SimpleNamespace(
        media_manager=staging,
        correlated_sends=ledger,
        channel_registry=registry,
        client=SimpleNamespace(session_name="default"),
    )
    entry.runtime_data = runtime
    runtime.outbound_observer = outbound.OutboundObserver(
        SimpleNamespace(
            bus=SimpleNamespace(async_fire=lambda _name, event: events.append(event))
        ),
        entry,
        registry,
    )
    backend = AsyncMock(return_value=api.WahaMessage("raw-id", "123@c.us"))
    monkeypatch.setattr(send, "async_send_staged_media", backend)
    route_calls = []

    async def route(conversation, transmit):
        route_calls.append(conversation)
        return await transmit("123@c.us", "direct", False)

    return entry, staging, backend, route, events, route_calls


async def call(entry, route, *, data=None, principal="admin-a"):
    return await send.async_send_media(
        entry, principal, data or DATA, async_route=route
    )


async def test_persist_before_send_replay_and_one_allowlisted_observation(
    tmp_path, monkeypatch
):
    store = FakeStore()
    entry, staging, backend, route, events, routes = await setup(
        tmp_path, monkeypatch, store=store
    )

    async def submit(*_args, **_kwargs):
        assert next(iter(store.data["records"].values()))["status"] == "pending"
        return api.WahaMessage("raw-id", "123@c.us")

    backend.side_effect = submit
    first = await call(entry, route)
    assert first["status"] == "accepted"
    assert first["message_id"] == "msg_safe"
    assert (await call(entry, route))["replayed"] is True
    assert backend.await_count == staging.opens == len(events) == 1
    assert len(routes) == 3
    event = events[0]
    assert event["message"]["attachments"][0]["sha256"] == staging.receipt.sha256
    for value in ("upload-a", "admin-a", "generated.pdf", "route-binding"):
        assert value not in str(store.data)
    assert "upload-a" not in str(events)
    assert "raw-id" not in str(first)


async def test_accepted_expired_bytes_restart_replay_and_unknown_id_rejection(
    tmp_path, monkeypatch
):
    store = FakeStore()
    entry, staging, backend, route, _events, _routes = await setup(
        tmp_path, monkeypatch, store=store
    )
    accepted = await call(entry, route)
    staging.expired = True
    staging.path.unlink()
    staging.receipts.clear()
    await entry.runtime_data.correlated_sends.async_stop()
    restored = manager(store)
    await restored.async_start()
    entry.runtime_data.correlated_sends = restored
    assert restored.persistence_available
    assert await call(entry, route) == {**accepted, "replayed": True}
    unknown = await call(entry, route, data={**DATA, "upload_id": "guessed-upload"})
    assert unknown["error_code"] == "media_not_found"
    backend.assert_awaited_once()
    assert staging.opens == 1


async def test_identical_restage_replays_and_alias_survives_expiry(
    tmp_path, monkeypatch
):
    store = FakeStore()
    entry, staging, backend, route, _events, _routes = await setup(
        tmp_path, monkeypatch, store=store
    )
    await call(entry, route)
    staging.receipts["upload-b"] = replace(staging.receipt, upload_id="upload-b")
    restaged = {**DATA, "upload_id": "upload-b"}
    assert (await call(entry, route, data=restaged))["replayed"]
    staging.receipts.clear()
    assert (await call(entry, route, data=restaged))["replayed"]
    backend.assert_awaited_once()


async def test_concurrent_identical_media_returns_pending_without_second_io(
    tmp_path, monkeypatch
):
    entry, _staging, backend, route, _events, _routes = await setup(
        tmp_path, monkeypatch
    )
    entered, release = asyncio.Event(), asyncio.Event()

    async def wait(*_args, **_kwargs):
        entered.set()
        await release.wait()
        return api.WahaMessage("raw-id", "123@c.us")

    backend.side_effect = wait
    first = asyncio.create_task(call(entry, route))
    await entered.wait()
    assert (await call(entry, route))["status"] == "pending"
    release.set()
    assert (await first)["status"] == "accepted"
    backend.assert_awaited_once()


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("caption", "changed"),
        ("origin", "human"),
        ("reply_to_message_id", "unknown-quote"),
    ],
)
async def test_payload_conflict_precedes_quote_and_network(
    tmp_path, monkeypatch, field, value
):
    entry, _staging, backend, route, _events, _routes = await setup(
        tmp_path, monkeypatch
    )
    await call(entry, route)
    result = await call(entry, route, data={**DATA, field: value})
    assert result["error_code"] == "request_conflict"
    backend.assert_awaited_once()


async def test_cross_text_media_keys_conflict_in_both_directions(tmp_path, monkeypatch):
    entry, _staging, backend, route, _events, _routes = await setup(
        tmp_path, monkeypatch
    )
    ledger = entry.runtime_data.correlated_sends
    text = AsyncMock(return_value="msg_text")
    await ledger.async_execute("direct_a", "request-a", {"message": "text"}, text)
    assert (await call(entry, route))["error_code"] == "request_conflict"
    backend.assert_not_awaited()
    media_data = {**DATA, "client_request_id": "media-key"}
    await call(entry, route, data=media_data)
    result = await ledger.async_execute(
        "direct_a", "media-key", {"message": "text"}, text
    )
    assert result["error_code"] == "request_conflict"
    text.assert_awaited_once()


@pytest.mark.parametrize("principal", ["admin-b", ""])
async def test_uploader_principal_is_required(tmp_path, monkeypatch, principal):
    entry, _staging, backend, route, _events, _routes = await setup(
        tmp_path, monkeypatch
    )
    result = await call(entry, route, principal=principal)
    assert result["error_code"] in {"not_authorized", "media_not_found"}
    backend.assert_not_awaited()


async def test_revocation_between_admission_and_send_fails_closed(
    tmp_path, monkeypatch
):
    entry, _staging, backend, _route, _events, _routes = await setup(
        tmp_path, monkeypatch
    )
    checks = 0

    async def revoked(_conversation, transmit):
        nonlocal checks
        checks += 1
        if checks == 2:
            raise correlated.CorrelatedSendError("invalid_conversation")
        return await transmit("123@c.us", "direct", False)

    assert (await call(entry, revoked))["error_code"] == "invalid_conversation"
    backend.assert_not_awaited()


async def test_timeout_unknown_replayed_after_restart_without_resend(
    tmp_path, monkeypatch
):
    store = FakeStore()
    entry, _staging, backend, route, _events, _routes = await setup(
        tmp_path, monkeypatch, store=store
    )
    backend.side_effect = TimeoutError
    assert (await call(entry, route))["status"] == "unknown"
    restored = manager(store)
    await restored.async_start()
    entry.runtime_data.correlated_sends = restored
    assert (await call(entry, route))["status"] == "unknown"
    backend.assert_awaited_once()


async def test_observer_failure_preserves_one_accepted_send_and_redaction(
    tmp_path, monkeypatch
):
    entry, _staging, backend, route, events, _routes = await setup(
        tmp_path, monkeypatch
    )
    redacted = {**DATA, "content_redacted": True}
    assert (await call(entry, route, data=redacted))["status"] == "accepted"
    assert "attachments" not in events[0]["message"]
    assert "caption" not in events[0]["message"]
    assert events[0]["content_redacted"] is True
    entry.runtime_data.outbound_observer._hass.bus.async_fire = lambda *_args: (
        _ for _ in ()
    ).throw(RuntimeError())
    assert (await call(entry, route, data={**DATA, "client_request_id": "other"}))[
        "status"
    ] == "accepted"
    assert backend.await_count == 2


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("kind", []),
        ("origin", {}),
        ("as_voice_note", 1),
        ("url", "https://evil.test"),
        ("client_request_id", "x" * 129),
    ],
)
async def test_untrusted_request_shapes_are_safe_failures(
    tmp_path, monkeypatch, field, value
):
    entry, _staging, backend, route, _events, _routes = await setup(
        tmp_path, monkeypatch
    )
    assert (await call(entry, route, data={**DATA, field: value}))[
        "error_code"
    ] == "invalid_request"
    backend.assert_not_awaited()


async def test_old_ledger_and_corrupt_extension_restore_fail_closed(
    tmp_path, monkeypatch
):
    store = FakeStore()
    entry, _staging, _backend, route, _events, _routes = await setup(
        tmp_path, monkeypatch, store=store
    )
    await call(entry, route)
    original = deepcopy(store.data)
    next(iter(store.data["records"].values()))["media"]["upload_tokens"] *= 100
    bad = manager(store)
    await bad.async_start()
    assert not bad.persistence_available
    store.data = original
    del next(iter(store.data["records"].values()))["media"]
    old = manager(store)
    await old.async_start()
    assert old.persistence_available


async def test_bounded_send_queue_revalidates_queued_routes_before_network(
    tmp_path,
    monkeypatch,
):
    entry, staging, backend, _route, _events, _routes = await setup(
        tmp_path, monkeypatch
    )
    entered, release = asyncio.Event(), asyncio.Event()
    valid = True

    async def route(_conversation, transmit):
        if not valid:
            raise correlated.CorrelatedSendError("invalid_conversation")
        return await transmit("123@c.us", "direct", False)

    async def wait(*_args, **_kwargs):
        entered.set()
        await release.wait()
        return api.WahaMessage("raw-id", "123@c.us")

    backend.side_effect = wait
    active = asyncio.create_task(call(entry, route))
    await entered.wait()
    queued = [
        asyncio.create_task(
            call(
                entry,
                route,
                data={
                    **DATA,
                    "client_request_id": f"queued-{index}",
                },
            )
        )
        for index in range(4)
    ]
    for _ in range(10):
        await asyncio.sleep(0)
    overflow = await call(entry, route, data={**DATA, "client_request_id": "overflow"})
    assert overflow["error_code"] == "busy"
    assert staging.opens == backend.await_count == 1
    valid = False
    release.set()
    assert (await active)["status"] == "accepted"
    assert all(
        result["error_code"] == "invalid_conversation"
        for result in await asyncio.gather(*queued)
    )
    backend.assert_awaited_once()


async def test_revocation_during_payload_preparation_fences_network(
    tmp_path, monkeypatch
):
    entry, _staging, backend, _route, events, _routes = await setup(
        tmp_path, monkeypatch
    )
    entered, release = asyncio.Event(), asyncio.Event()
    valid = True

    async def prepare(*_args, **_kwargs):
        entered.set()
        await release.wait()
        return {"file": {"data": "private-backend-only"}}

    async def route(_conversation, transmit):
        if not valid:
            raise media.MediaError("invalid_conversation")
        return await transmit("123@c.us", "direct", False)

    monkeypatch.setattr(send, "async_prepare_staged_media", prepare)
    pending = asyncio.create_task(call(entry, route))
    await entered.wait()
    valid = False
    release.set()
    assert (await pending)["error_code"] == "invalid_conversation"
    backend.assert_not_awaited()
    assert events == []


@pytest.mark.parametrize(
    ("failure", "code"),
    [
        (ValueError("integrity"), "invalid_media"),
        (OSError("disk"), "storage_unavailable"),
    ],
)
async def test_payload_preparation_failure_is_definite_and_never_submits(
    tmp_path, monkeypatch, failure, code
):
    entry, _staging, backend, route, events, _routes = await setup(
        tmp_path, monkeypatch
    )
    monkeypatch.setattr(
        send, "async_prepare_staged_media", AsyncMock(side_effect=failure)
    )
    result = await call(entry, route)
    assert result["status"] == "failed"
    assert result["error_code"] == code
    assert (await call(entry, route))["replayed"] is True
    backend.assert_not_awaited()
    assert events == []


@pytest.mark.parametrize(
    ("state", "code"),
    [("stopped", "channel_unavailable"), ("unavailable", "persistence_unavailable")],
)
async def test_new_restage_alias_requires_live_writer_but_known_replay_stays_read_only(
    tmp_path, monkeypatch, state, code
):
    store = FakeStore()
    entry, staging, backend, route, events, _routes = await setup(
        tmp_path, monkeypatch, store=store
    )
    accepted = await call(entry, route)
    staging.receipts["upload-b"] = replace(staging.receipt, upload_id="upload-b")
    ledger = entry.runtime_data.correlated_sends
    if state == "stopped":
        await ledger.async_stop()
    else:
        ledger.persistence_available = False
    snapshot, saves = deepcopy(store.data), store.save_count
    known = await call(entry, route)
    assert known == {**accepted, "replayed": True}
    restaged = await call(entry, route, data={**DATA, "upload_id": "upload-b"})
    assert restaged["status"] == "failed"
    assert restaged["error_code"] == code
    assert restaged["replayed"] is False
    conflict = await call(entry, route, data={**DATA, "caption": "Other payload"})
    assert conflict["error_code"] == "request_conflict"
    assert ledger._snapshot() == snapshot
    assert store.data == snapshot
    assert store.save_count == saves
    assert staging.opens == len(events) == 1
    backend.assert_awaited_once()


async def test_old_preflight_cannot_overwrite_replacement_receipts_with_restage_alias(
    tmp_path, monkeypatch
):
    hass, store = SimpleNamespace(data={}), FakeStore()
    entry, staging, backend, route, _events, _routes = await setup(
        tmp_path, monkeypatch, store=store, hass=hass
    )
    old = entry.runtime_data.correlated_sends
    accepted = await call(entry, route)
    staging.receipts["upload-b"] = replace(staging.receipt, upload_id="upload-b")
    entered, release = asyncio.Event(), asyncio.Event()

    async def paused_preflight(conversation, transmit):
        entered.set()
        await release.wait()
        return await route(conversation, transmit)

    late = asyncio.create_task(
        call(entry, paused_preflight, data={**DATA, "upload_id": "upload-b"})
    )
    await asyncio.wait_for(entered.wait(), 2)
    try:
        # The old call captured its runtime/ledger before pausing. It has not
        # been admitted, so old.stop completes and the replacement can load.
        await old.async_stop()
        replacement = correlated.CorrelatedSendManager(hass, entry, store=store)
        await replacement.async_start()
        assert replacement.persistence_available
        entry.runtime_data = SimpleNamespace(
            **{**vars(entry.runtime_data), "correlated_sends": replacement}
        )
        newer_data = {**DATA, "client_request_id": "replacement-request"}
        newer = await call(entry, route, data=newer_data)
        assert newer["status"] == "accepted"
        assert len(store.data["records"]) == 2
        snapshot, saves = deepcopy(store.data), store.save_count
    finally:
        release.set()
    late_result = await late
    assert late_result["status"] == "failed"
    assert late_result["error_code"] == "channel_unavailable"
    assert store.data == snapshot
    assert store.save_count == saves
    assert backend.await_count == staging.opens == 2

    # A second reload must retain both accepted receipts. Removing the bytes
    # forces genuine durable replays, rather than another staged submission.
    restarted = correlated.CorrelatedSendManager(hass, entry, store=store)
    await restarted.async_start()
    entry.runtime_data.correlated_sends = restarted
    staging.receipts.clear()
    staging.expired = True
    staging.path.unlink()
    assert await call(entry, route) == {**accepted, "replayed": True}
    assert await call(entry, route, data=newer_data) == {**newer, "replayed": True}
    assert store.data == snapshot
    assert store.save_count == saves
    assert backend.await_count == staging.opens == 2


async def test_live_restage_alias_quota_and_saved_alias_replay_remain_bounded(
    tmp_path, monkeypatch
):
    store = FakeStore()
    entry, staging, backend, route, _events, _routes = await setup(
        tmp_path, monkeypatch, store=store
    )
    accepted = await call(entry, route)
    for index in range(1, 8):
        upload_id = f"identical-restage-{index}"
        staging.receipts[upload_id] = replace(staging.receipt, upload_id=upload_id)
        assert await call(entry, route, data={**DATA, "upload_id": upload_id}) == {
            **accepted,
            "replayed": True,
        }
    snapshot, saves = deepcopy(store.data), store.save_count
    staging.receipts["one-too-many"] = replace(
        staging.receipt, upload_id="one-too-many"
    )
    overflow = await call(entry, route, data={**DATA, "upload_id": "one-too-many"})
    assert overflow["error_code"] == "busy"
    assert overflow["replayed"] is False
    conflict = await call(
        entry, route, data={**DATA, "upload_id": "one-too-many", "caption": "Changed"}
    )
    assert conflict["error_code"] == "request_conflict"
    staging.receipts.clear()
    await entry.runtime_data.correlated_sends.async_stop()
    assert await call(
        entry, route, data={**DATA, "upload_id": "identical-restage-7"}
    ) == {**accepted, "replayed": True}
    assert store.data == snapshot
    assert store.save_count == saves
    assert (
        len(next(iter(store.data["records"].values()))["media"]["upload_tokens"]) == 8
    )
    backend.assert_awaited_once()
