"""Exercise real media persistence through the integration lifecycle boundaries."""

import asyncio
import json
import logging
from functools import partial
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from ._loader import INTEGRATION_PATH
from .test_channel_services import _functions
from .test_media_core import PDF, chunks, manager, media


def _restart(tmp_path, *, entry_id="entry"):
    hass = SimpleNamespace(
        data={},
        config=SimpleNamespace(path=lambda *p: str(tmp_path.joinpath(*p))),
        async_add_executor_job=lambda f, *a: asyncio.to_thread(f, *a),
    )
    entry = SimpleNamespace(entry_id=entry_id, options={"media_enabled": False})
    authorize = AsyncMock(return_value="route")
    ready = AsyncMock(return_value=True)
    handlers = _functions(
        INTEGRATION_PATH / "__init__.py",
        {"_async_setup_media_manager", "async_remove_entry"},
        {
            "MediaManager": media.MediaManager,
            "MediaError": media.MediaError,
            "partial": partial,
            "async_authorize_media": authorize,
            "async_media_backend_ready": ready,
            "media_observation_binding": lambda *_args, **_kwargs: "route",
            "_LOGGER": logging.getLogger(__name__),
        },
    )
    return hass, entry, handlers, authorize, ready


async def _persist_two_entries(tmp_path):
    first = await manager(tmp_path)
    other = await manager(tmp_path, entry_id="other", hass=first.hass)
    receipts = [
        await owner.async_stage(
            "direct", "admin", chunks(), "application/pdf", "test.pdf"
        )
        for owner in (first, other)
    ]
    first.store.timer.cancel()
    return first.store, receipts


@pytest.mark.parametrize("retained_manager", [False, True])
async def test_entry_removal_restores_shared_index_before_mutation(
    tmp_path, retained_manager
):
    original, (target, other) = await _persist_two_entries(tmp_path)
    original_other = original.records[other.upload_id].copy()
    target_path = original.path / original.records[target.upload_id]["file"]
    other_path = original.path / original_other["file"]
    hass, entry, handlers, authorize, ready = _restart(tmp_path)
    if retained_manager:
        disabled = media.MediaManager(
            hass, entry, None, authorize=authorize, backend_ready=ready
        )
        entry.runtime_data = SimpleNamespace(media_manager=disabled)
        assert not disabled.store.started
    await handlers["async_remove_entry"](hass, entry)
    restored = hass.data[media.STORE_KEY]
    persisted = json.loads((original.path / "index.json").read_text())
    assert persisted == {
        "secret": original.secret,
        "records": {other.upload_id: original_other},
    }
    assert not target_path.exists()
    assert other_path.read_bytes() == PDF
    assert restored.started
    assert restored.timer is not None
    assert not retained_manager or not disabled.enabled
    authorize.assert_not_awaited()
    ready.assert_not_awaited()
    restored.timer.cancel()


async def test_disabled_restart_schedules_expiry_without_enabling_transfers(
    tmp_path, monkeypatch
):
    original, (target, other) = await _persist_two_entries(tmp_path)
    deadline = min(target.expires_at, other.expires_at)
    paths = [original.path / record["file"] for record in original.records.values()]
    monkeypatch.setattr(media.time, "time", lambda: deadline - 0.05)
    hass, entry, handlers, authorize, ready = _restart(tmp_path)
    disabled = await handlers["_async_setup_media_manager"](hass, entry, None)
    assert disabled.store.secret == original.secret
    assert disabled.store.records == original.records
    assert disabled.store.started
    assert not disabled.enabled
    assert disabled.store.timer is not None
    assert disabled.store.timer.when() - asyncio.get_running_loop().time() <= 0.05
    cleaned = asyncio.Event()
    cleanup = disabled.store.cleanup

    async def observe_cleanup():
        await cleanup()
        cleaned.set()

    monkeypatch.setattr(disabled.store, "cleanup", observe_cleanup)
    monkeypatch.setattr(
        media.time, "time", lambda: max(target.expires_at, other.expires_at) + 1
    )
    await asyncio.wait_for(cleaned.wait(), 2)
    assert all(not path.exists() for path in paths)
    assert all("file" not in record for record in disabled.store.records.values())
    assert not disabled.enabled
    with pytest.raises(media.MediaError, match="media_disabled"):
        await disabled.async_stage(
            "direct", "admin", chunks(), "application/pdf", "test.pdf"
        )
    authorize.assert_not_awaited()
    ready.assert_not_awaited()
    disabled.store.timer.cancel()


@pytest.mark.parametrize("damage", ["corrupt", "unreadable", "persist_failure"])
async def test_unavailable_index_fails_closed_during_setup_and_removal(
    tmp_path, monkeypatch, damage
):
    original, _receipts = await _persist_two_entries(tmp_path)
    manifest = original.path / "index.json"
    if damage == "corrupt":
        manifest.write_text('{"secret":"broken","records":{}}')
    index_bytes = manifest.read_bytes()
    cache_bytes = {path.name: path.read_bytes() for path in original.path.glob("*.bin")}
    if damage == "unreadable":
        read_text = type(manifest).read_text

        def deny_index(path, *args, **kwargs):
            if path == manifest:
                raise PermissionError("index unreadable")
            return read_text(path, *args, **kwargs)

        monkeypatch.setattr(type(manifest), "read_text", deny_index)
    elif damage == "persist_failure":

        def deny_persist(_store):
            raise OSError("index write unavailable")

        monkeypatch.setattr(media._Store, "_write", deny_persist)
    hass, entry, handlers, _authorize, _ready = _restart(tmp_path)
    disabled = await handlers["_async_setup_media_manager"](hass, entry, None)
    entry.runtime_data = SimpleNamespace(media_manager=disabled)
    assert not disabled.enabled
    assert not disabled.store.started
    with pytest.raises(media.MediaError, match="storage_unavailable"):
        await handlers["async_remove_entry"](hass, entry)
    assert manifest.read_bytes() == index_bytes
    assert {
        path.name: path.read_bytes() for path in original.path.glob("*.bin")
    } == cache_bytes
    assert disabled.store.timer is None


async def test_removal_without_existing_storage_initializes_empty_store(tmp_path):
    hass, entry, handlers, authorize, ready = _restart(tmp_path)
    await handlers["async_remove_entry"](hass, entry)
    store = hass.data[media.STORE_KEY]
    assert store.started
    assert store.timer is None
    assert json.loads((store.path / "index.json").read_text()) == {
        "secret": store.secret,
        "records": {},
    }
    authorize.assert_not_awaited()
    ready.assert_not_awaited()
