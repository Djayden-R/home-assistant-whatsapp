"""Synthetic bounded store/metadata contract, without Home Assistant or WhatsApp."""

import asyncio
import hashlib
import json
import os
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from ._loader import load_integration_module

media = load_integration_module("media")
backend = load_integration_module("media_backend")
PDF = b"%PDF-1.7\n1 0 obj<</Type/Catalog>>endobj\n%%EOF\n"


def payload(*, info="ptt", mime="audio/ogg", document=False):
    return {
        "id": "false_111@c.us_SYNTHETIC",
        "from": "111@c.us",
        "fromMe": False,
        "hasMedia": True,
        "media": {"mimetype": mime, "filename": "voice.ogg"},
        "_data": {
            "Info": {"MediaType": info},
            "Message": {
                "documentMessage" if document else "audioMessage": {
                    "seconds": 12,
                    "fileLength": 42,
                },
            },
        },
    }


async def chunks(data=PDF):
    for start in range(0, len(data), media.CHUNK_BYTES):
        yield data[start : start + media.CHUNK_BYTES]


async def manager(
    tmp_path, *, entry_id="entry", hass=None, binding="route", observe=True
):
    if hass is None:
        hass = SimpleNamespace(
            data={},
            config=SimpleNamespace(path=lambda *p: str(tmp_path.joinpath(*p))),
            async_add_executor_job=lambda f, *a: asyncio.to_thread(f, *a),
        )
    authorize = AsyncMock(return_value=binding)
    result = media.MediaManager(
        hass,
        SimpleNamespace(entry_id=entry_id),
        None,
        authorize=authorize,
        backend_ready=AsyncMock(return_value=True),
        observe_binding=(lambda conversation, **kwargs: binding) if observe else None,
    )
    await result.async_start()
    return result


@pytest.mark.parametrize(
    ("info", "expected"), [("ptt", True), ("audio", False), (None, None), ("PTT", None)]
)
def test_voice_is_transport_fact_not_ogg_mime(info, expected):
    result = media.media_metadata(payload(info=info))
    assert result["voice_note"] is expected
    assert result["kind"] == "audio"
    assert result["duration_ms"] == 12000


def test_document_with_audio_mime_stays_document_and_metadata_is_safe():
    data = payload(info="document", document=True)
    data["media"]["filename"] = "../../escape\r\n.ogg"
    data["media"]["url"] = "http://secret.invalid/api/files/secret"
    result = media.media_metadata(data)
    assert result["kind"] == "file"
    assert result["voice_note"] is None
    assert result["file_name"] == "escape.ogg"
    assert "url" not in result
    data["_data"]["Message"] = {}
    assert media.media_metadata(data)["size_bytes"] is None


async def test_observation_is_stable_persistent_metadata_only_without_network(tmp_path):
    result = await manager(tmp_path)
    first = await result.async_observe(payload(), "msg", "direct")
    second = await result.async_observe(payload(), "msg", "direct")
    assert first == second
    assert first[0]["retrievable"]
    assert "111" not in first[0]["attachment_id"]
    result.authorize.assert_not_awaited()
    result.backend_ready.assert_not_awaited()
    restored = await manager(tmp_path)
    third = await restored.async_observe(payload(), "msg", "direct")
    assert third == first
    assert len(restored.store.records) == 1
    assert os.stat(restored.store.path).st_mode & 0o777 == 0o700
    assert os.stat(restored.store.path / "index.json").st_mode & 0o777 == 0o600


async def test_reference_capacity_never_evicts_live_binding(tmp_path, monkeypatch):
    result = await manager(tmp_path)
    monkeypatch.setattr(media, "REFERENCE_MAX_ITEMS", 1)
    first = await result.async_observe(payload(), "msg", "direct")
    second = await result.async_observe(payload(), "other", "direct")
    assert first[0]["retrievable"]
    assert not second[0]["retrievable"]
    assert first[0]["attachment_id"] in result.store.records


async def test_upload_does_not_send_and_binds_principal_destination_digest(tmp_path):
    result = await manager(tmp_path)
    receipt = await result.async_stage(
        "direct", "admin", chunks(), "application/pdf", "../../test.pdf"
    )
    assert receipt.sha256 == hashlib.sha256(PDF).hexdigest()
    assert receipt.file_name == "test.pdf"
    assert receipt.kind == "file"
    async with result.async_open_upload(receipt.upload_id, "direct", "admin") as (
        same,
        path,
    ):
        assert same == receipt
        assert path.read_bytes() == PDF
        assert path.name != receipt.file_name
        assert os.stat(path).st_mode & 0o777 == 0o600
    for conversation, user in (("other", "admin"), ("direct", "guest")):
        with pytest.raises(media.MediaError, match="media_not_found"):
            result.get_upload(receipt.upload_id, conversation, user)
    other_entry = await manager(tmp_path, entry_id="entry_other", hass=result.hass)
    with pytest.raises(media.MediaError, match="media_not_found"):
        other_entry.get_upload(receipt.upload_id, "direct", "admin")


async def test_stage_rejects_dishonest_type_oversize_and_cancellation(
    tmp_path, monkeypatch
):
    result = await manager(tmp_path)
    for data, mime, code in (
        (PDF, "image/png", "invalid_media"),
        (b"not a pdf", "application/pdf", "unsupported_media_type"),
        (b"", "application/pdf", "invalid_media"),
    ):
        with pytest.raises(media.MediaError, match=code):
            await result.async_stage("direct", "admin", chunks(data), mime, "test")
    monkeypatch.setattr(media, "MAX_BYTES", 8)
    with pytest.raises(media.MediaError, match="media_too_large"):
        await result.async_stage("direct", "admin", chunks(), "application/pdf", "test")
    monkeypatch.setattr(media, "MAX_BYTES", 25 * 1024 * 1024)
    entered = asyncio.Event()

    async def interrupted():
        yield PDF
        entered.set()
        await asyncio.Event().wait()

    task = asyncio.create_task(
        result.async_stage("direct", "admin", interrupted(), "application/pdf", "test")
    )
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not list(result.store.path.glob("*.bin"))
    assert result.store.reservations == 0
    assert not result.store.records


async def test_route_rebind_fences_upload_after_stream_and_before_read(tmp_path):
    result = await manager(tmp_path)

    async def changed():
        yield PDF
        result.authorize.return_value = "rebound"

    with pytest.raises(media.MediaError, match="media_not_found"):
        await result.async_stage(
            "direct", "admin", changed(), "application/pdf", "test"
        )
    assert not list(result.store.path.glob("*.bin"))
    result.authorize.return_value = "route"
    receipt = await result.async_stage(
        "direct", "admin", chunks(), "application/pdf", "test"
    )
    result.authorize.return_value = "rebound"
    with pytest.raises(media.MediaError, match="media_not_found"):
        async with result.async_open_upload(receipt.upload_id, "direct", "admin"):
            pytest.fail("revoked route opened")


async def test_integrity_tampering_fails_before_send(tmp_path):
    result = await manager(tmp_path)
    receipt = await result.async_stage(
        "direct", "admin", chunks(), "application/pdf", "test"
    )
    path = result.store.path / result.store.records[receipt.upload_id]["file"]
    path.write_bytes(b"x" * len(PDF))
    with pytest.raises(media.MediaError, match="invalid_media"):
        async with result.async_open_upload(receipt.upload_id, "direct", "admin"):
            pytest.fail("corrupt upload opened")


async def test_expiry_deadline_does_not_extend_and_cleanup_preserves_unrelated_files(
    tmp_path, monkeypatch
):
    result = await manager(tmp_path)
    receipt = await result.async_stage(
        "direct", "admin", chunks(), "application/pdf", "test"
    )
    path = result.store.path / result.store.records[receipt.upload_id]["file"]
    unrelated = result.store.path / "do-not-delete.txt"
    unrelated.write_text("unrelated")
    deadline = receipt.expires_at
    monkeypatch.setattr(media.time, "time", lambda: deadline + 1)
    await result.store.cleanup()
    assert not path.exists()
    assert unrelated.exists()
    with pytest.raises(media.MediaError, match="media_expired"):
        result.get_upload(receipt.upload_id, "direct", "admin")
    assert (
        result.get_upload(
            receipt.upload_id, "direct", "admin", allow_expired=True
        ).sha256
        == receipt.sha256
    )
    monkeypatch.setattr(
        media.time, "time", lambda: deadline + media.RETENTION_SECONDS + 1
    )
    await result.store.cleanup()
    assert not result.store.records


async def test_global_quota_reservation_covers_concurrent_entries(
    tmp_path, monkeypatch
):
    result = await manager(tmp_path)
    other = await manager(tmp_path, entry_id="other", hass=result.hass)
    monkeypatch.setattr(media, "CACHE_MAX_FILES", 1)
    paused = asyncio.Event()
    release = asyncio.Event()

    async def slow():
        yield PDF
        paused.set()
        await release.wait()

    task = asyncio.create_task(
        result.async_stage("direct", "admin", slow(), "application/pdf", "test")
    )
    await paused.wait()
    with pytest.raises(media.MediaError, match="storage_unavailable"):
        await other.async_stage("direct", "admin", chunks(), "application/pdf", "test")
    release.set()
    await task
    assert result.store.reservations == 0


async def test_queue_is_bounded_and_stop_drains_partial_uploads(tmp_path):
    result = await manager(tmp_path)
    entered = asyncio.Event()

    async def slow():
        entered.set()
        await asyncio.Event().wait()
        yield PDF

    tasks = [
        asyncio.create_task(
            result.async_stage("direct", "admin", slow(), "application/pdf", "test")
        )
        for _ in range(media.QUEUE_MAX + 1)
    ]
    await entered.wait()
    await asyncio.sleep(0)
    with pytest.raises(media.MediaError, match="busy"):
        await result.async_stage("direct", "admin", chunks(), "application/pdf", "test")
    await result.async_stop()
    assert all(t.done() for t in tasks)
    assert not list(result.store.path.glob("*.bin"))
    assert result.store.reservations == 0


async def test_startup_rejects_corrupt_paths_without_deleting_unrelated_files(tmp_path):
    result = await manager(tmp_path)
    receipt = await result.async_stage(
        "direct", "admin", chunks(), "application/pdf", "test"
    )
    manifest = result.store.path / "index.json"
    content = json.loads(manifest.read_text())
    content["records"][receipt.upload_id]["file"] = "../../victim"
    manifest.write_text(json.dumps(content))
    with pytest.raises(media.MediaError, match="storage_unavailable"):
        await manager(tmp_path)


async def test_restart_reconciles_missing_cache_and_removes_only_owned_orphans(
    tmp_path,
):
    result = await manager(tmp_path)
    attachment = (await result.async_observe(payload(), "msg", "direct"))[0]
    orphan = result.store.path / ("a" * 32 + ".bin")
    orphan.write_bytes(PDF)
    unrelated = result.store.path / "keep.bin"
    unrelated.write_bytes(PDF)
    restored = await manager(tmp_path)
    assert attachment["attachment_id"] in restored.store.records
    assert not orphan.exists()
    assert unrelated.exists()


async def test_inbound_retrieval_coalesces_and_enforces_exact_scope(
    tmp_path, monkeypatch
):
    from contextlib import asynccontextmanager

    result = await manager(tmp_path)
    attachment = (
        await result.async_observe(
            payload(info="document", mime="application/pdf", document=True),
            "msg",
            "direct",
        )
    )[0]
    calls = 0
    entered = asyncio.Event()
    release = asyncio.Event()

    @asynccontextmanager
    async def download(client, record):
        nonlocal calls
        calls += 1
        entered.set()
        await release.wait()
        yield {"mime_type": "application/pdf"}, chunks()

    monkeypatch.setattr(backend, "async_download", download)
    first = asyncio.create_task(
        result.async_get_media("direct", "msg", attachment["attachment_id"])
    )
    await entered.wait()
    second = asyncio.create_task(
        result.async_get_media("direct", "msg", attachment["attachment_id"])
    )
    release.set()
    one, two = await asyncio.gather(first, second)
    assert one == two
    assert calls == 1
    assert one["attachment"]["sha256"] == hashlib.sha256(PDF).hexdigest()
    assert result.store.reservations == 0
    assert "url" not in json.dumps(one)
    async with result.async_download("direct", attachment["attachment_id"]) as (
        metadata,
        path,
    ):
        assert path.read_bytes() == PDF
        assert metadata["size_bytes"] == len(PDF)
    for conversation, message in (("elsewhere", "msg"), ("direct", "other")):
        with pytest.raises(media.MediaError, match="media_not_found"):
            await result.async_get_media(
                conversation, message, attachment["attachment_id"]
            )
    with pytest.raises(media.MediaError, match="media_too_large"):
        await result.async_get_media("direct", "msg", attachment["attachment_id"], 2)


def ogg_opus():
    """Generate a small valid framed Opus stream using synthetic silence."""

    def page(packet, sequence, flags, granule):
        header = bytearray(
            b"OggS\x00"
            + bytes([flags])
            + granule.to_bytes(8, "little")
            + (42).to_bytes(4, "little")
            + sequence.to_bytes(4, "little")
            + b"\0" * 4
            + b"\x01"
            + bytes([len(packet)])
        )
        full = header + packet
        full[22:26] = media._ogg_crc(full).to_bytes(4, "little")
        return bytes(full)

    head = b"OpusHead\x01\x01\x00\x00\x80\xbb\x00\x00\x00\x00\x00"
    tags = b"OpusTags\x04\x00\x00\x00test\x00\x00\x00\x00"
    return page(head, 0, 2, 0) + page(tags, 1, 0, 0) + page(b"\xf8\xff\xfe", 2, 4, 960)


async def test_native_voice_staging_verifies_full_ogg_not_substring(tmp_path):
    result = await manager(tmp_path)
    valid = ogg_opus()
    receipt = await result.async_stage(
        "direct", "admin", chunks(valid), "audio/ogg", "voice.ogg"
    )
    assert receipt.native_opus
    for invalid in (
        b"OggSgarbage OpusHead",
        valid[:-1],
        valid.replace(b"OpusTags", b"OpusFake"),
        valid[:47],
        valid + b"junk",
    ):
        with pytest.raises(media.MediaError, match="invalid_media"):
            await result.async_stage(
                "direct", "admin", chunks(invalid), "audio/ogg", "voice.ogg"
            )
    assert len(list(result.store.path.glob("*.bin"))) == 1
    assert result.store.reservations == 0


async def test_observation_duplicate_cannot_rebind_raw_sender_chat_or_route(tmp_path):
    result = await manager(tmp_path)
    first = await result.async_observe(payload(), "msg", "direct")
    changed = payload() | {"participant": "222@c.us"}
    assert not (await result.async_observe(changed, "msg", "direct"))[0]["retrievable"]
    assert not (
        await result.async_observe(payload(), "msg", "direct", raw_chat_id="222@c.us")
    )[0]["retrievable"]
    result.observe_binding = lambda conversation, **kwargs: "rebound"
    assert not (await result.async_observe(payload(), "msg", "direct"))[0][
        "retrievable"
    ]
    assert result.store.records[first[0]["attachment_id"]]["route_binding"] == "route"


async def test_admin_revocation_during_upload_prevents_commit(tmp_path):
    result = await manager(tmp_path)
    authorized = True

    async def admin(_principal):
        return authorized

    async def revoked():
        nonlocal authorized
        yield PDF
        authorized = False

    with pytest.raises(media.MediaError, match="not_authorized"):
        await result.async_stage(
            "direct",
            "admin",
            revoked(),
            "application/pdf",
            "x",
            authorize_principal=admin,
        )
    assert not result.store.records
    assert not list(result.store.path.glob("*.bin"))


async def test_repeated_cancellation_drains_resource_owning_executor(tmp_path):
    import threading

    result = await manager(tmp_path)
    entered, release = threading.Event(), threading.Event()
    finished = threading.Event()

    def operation():
        entered.set()
        release.wait(2)
        finished.set()

    task = asyncio.create_task(media._io(result.hass, operation))
    assert await asyncio.to_thread(entered.wait, 2)
    task.cancel()
    await asyncio.sleep(0)
    task.cancel()
    await asyncio.sleep(0)
    assert not task.done()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert finished.is_set()


async def test_actual_guest_registry_routes_and_media_share_membership_binding(
    tmp_path, monkeypatch
):
    from contextlib import asynccontextmanager
    from copy import deepcopy
    from functools import partial
    from time import time

    from .test_media_routes import _entry

    registry_module = load_integration_module("guest_registry")
    routes = load_integration_module("media_routes")

    class GuestStore:
        data = None

        async def async_load(self):
            return deepcopy(self.data)

        async def async_save(self, data):
            self.data = deepcopy(data)

    bot, guest, group = "390000000099@c.us", "390000000001@c.us", "120000000000@g.us"
    guests = registry_module.GuestRegistry(None, "entry", store=GuestStore())
    await guests.async_initialize_empty()
    await guests.async_start()
    await guests.async_bind_account(bot)
    await guests.async_set_group(group)
    await guests.async_set_provisioning_status("ready")
    roster = [
        {
            "aliases": [guest],
            "aliases_verified": False,
            "kind": "guest",
            "role": "participant",
        }
    ]

    async def reconcile(roster):
        await guests.async_reconcile(
            roster,
            observed_at=time(),
            verified_bot_jid=bot,
            security_verified=True,
            bot_admin_verified=True,
        )

    await reconcile(roster)
    entry = _entry()
    entry.entry_id = "entry"
    entry.runtime_data.channel_registry.resolve_conversation = lambda _id: None
    entry.runtime_data.guest_registry = guests
    entry.runtime_data.guest_manager = SimpleNamespace(
        async_reconcile=AsyncMock(return_value={"ready": True}),
        async_verify_group_destination=AsyncMock(return_value=True),
        async_verify_guest_destination=AsyncMock(return_value=True),
    )
    harness = await manager(tmp_path)
    transport = media.MediaManager(
        harness.hass,
        entry,
        None,
        authorize=partial(routes.async_authorize_media, entry),
        backend_ready=partial(routes.async_media_backend_ready, entry),
        observe_binding=partial(routes.media_observation_binding, entry),
    )
    await transport.async_start()
    conversation = guests.snapshot()["conversation_id"]
    member = guests.resolve_member(guest, occurred_at=time())
    incoming = payload(info="document", mime="application/pdf", document=True) | {
        "from": group,
        "participant": guest,
    }
    attachment = (
        await transport.async_observe(
            incoming,
            "msg",
            conversation,
            raw_chat_id=group,
            raw_participant=guest,
            participant_id=member["participant_id"],
        )
    )[0]
    assert attachment["retrievable"]

    @asynccontextmanager
    async def fetch(*args):
        yield {"mime_type": "application/pdf"}, chunks()

    monkeypatch.setattr(backend, "async_download", fetch)
    assert (
        await transport.async_get_media(
            conversation, "msg", attachment["attachment_id"]
        )
    )["status"] == "ready"
    await reconcile([])
    with pytest.raises(media.MediaError):
        await transport.async_get_media(
            conversation, "msg", attachment["attachment_id"]
        )
    await reconcile(roster)
    assert (
        guests.resolve_member(guest, occurred_at=time())["membership_id"]
        != member["membership_id"]
    )
    with pytest.raises(media.MediaError, match="media_not_found"):
        await transport.async_get_media(
            conversation, "msg", attachment["attachment_id"]
        )


@pytest.mark.parametrize("operation", ["close", "unlink"])
async def test_repeated_cancel_during_partial_cleanup_drains_whole_sequence(
    tmp_path, operation
):
    result = await manager(tmp_path)
    waiting, cleanup_started, release = (
        asyncio.Event(),
        asyncio.Event(),
        asyncio.Event(),
    )
    executor = result.hass.async_add_executor_job
    owned_streams = []

    async def stalled_executor(function, *args):
        if function.__name__ == operation:
            if operation == "close":
                owned_streams.append(function.__self__)
            cleanup_started.set()
            await release.wait()
        return await executor(function, *args)

    result.hass.async_add_executor_job = stalled_executor

    async def interrupted():
        yield PDF
        waiting.set()
        await asyncio.Event().wait()

    task = asyncio.create_task(
        result.async_stage("direct", "admin", interrupted(), "application/pdf", "x.pdf")
    )
    await asyncio.wait_for(waiting.wait(), 2)
    task.cancel()
    await asyncio.wait_for(cleanup_started.wait(), 2)
    task.cancel()
    await asyncio.sleep(0)
    task.cancel()
    await asyncio.sleep(0)
    assert not task.done()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert result.store.reservations == 0
    assert not list(result.store.path.glob("*.bin"))
    assert not result.store.records
    assert not result.store.leases
    assert not result._operations
    assert all(stream.closed for stream in owned_streams)


@pytest.mark.parametrize("phase", ["stage", "fetch", "lease"])
async def test_repeated_cancel_during_unlink_preserves_budget_and_lease_accounting(
    tmp_path, monkeypatch, phase
):
    from contextlib import asynccontextmanager
    from time import time

    result = await manager(tmp_path)
    body_waiting, unlink_started, release = (
        asyncio.Event(),
        asyncio.Event(),
        asyncio.Event(),
    )
    executor = result.hass.async_add_executor_job

    async def stalled_executor(function, *args):
        if function.__name__ == "unlink":
            unlink_started.set()
            await release.wait()
        return await executor(function, *args)

    if phase == "stage":

        async def authorize(conversation, **kwargs):
            if kwargs:
                body_waiting.set()
                await asyncio.Event().wait()
            return "route"

        result.authorize = authorize
        task = asyncio.create_task(
            result.async_stage("direct", "admin", chunks(), "application/pdf", "x.pdf")
        )
    elif phase == "fetch":
        attachment = (
            await result.async_observe(
                payload(info="document", mime="application/pdf", document=True),
                "msg",
                "direct",
            )
        )[0]
        count = 0

        async def authorize(conversation, **kwargs):
            nonlocal count
            count += 1
            if count == 4:
                body_waiting.set()
                await asyncio.Event().wait()
            return "route"

        result.authorize = authorize

        @asynccontextmanager
        async def download(*args):
            yield {"mime_type": "application/pdf"}, chunks()

        monkeypatch.setattr(backend, "async_download", download)
        task = asyncio.create_task(
            result.async_get_media("direct", "msg", attachment["attachment_id"])
        )
    else:
        receipt = await result.async_stage(
            "direct", "admin", chunks(), "application/pdf", "x.pdf"
        )

        async def hold_lease():
            async with result.async_open_upload(receipt.upload_id, "direct", "admin"):
                result.store.records[receipt.upload_id]["expires_at"] = time() - 1
                body_waiting.set()
                await asyncio.Event().wait()

        task = asyncio.create_task(hold_lease())
    result.hass.async_add_executor_job = stalled_executor
    await asyncio.wait_for(body_waiting.wait(), 2)
    target = next(iter(result._fetches.values())) if phase == "fetch" else task
    target.cancel()
    await asyncio.wait_for(unlink_started.wait(), 2)
    target.cancel()
    await asyncio.sleep(0)
    target.cancel()
    await asyncio.sleep(0)
    assert not target.done()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert result.store.reservations == 0
    assert not list(result.store.path.glob("*.bin"))
    assert not result.store.leases
    assert not result._operations
    if phase == "stage":
        assert not result.store.records


async def test_announced_oversize_never_requests_backend_download(
    tmp_path, monkeypatch
):
    result = await manager(tmp_path)
    data = payload()
    data["_data"]["Message"]["audioMessage"]["fileLength"] = media.MAX_BYTES + 1
    attachment = (await result.async_observe(data, "msg", "direct"))[0]
    download = AsyncMock()
    monkeypatch.setattr(backend, "async_download", download)
    with pytest.raises(media.MediaError, match="media_too_large"):
        await result.async_get_media("direct", "msg", attachment["attachment_id"])
    download.assert_not_called()
    assert result.store.reservations == 0
