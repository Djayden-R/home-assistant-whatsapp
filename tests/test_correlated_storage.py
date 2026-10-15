"""Exercise durability against real HA Store, file helpers, and executor I/O."""

import asyncio
import json
import threading
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

pytest.importorskip("homeassistant")

from homeassistant.core import CoreState, HomeAssistant  # noqa: E402
from homeassistant.helpers.storage import Store  # noqa: E402
from homeassistant.util import file as file_util  # noqa: E402
from homeassistant.util.json import SerializationError  # noqa: E402

from ._loader import load_integration_module  # noqa: E402

correlated = load_integration_module("correlated")
REQUEST = {"message": "Private content", "origin": "application"}


def _manager(hass):
    return correlated.CorrelatedSendManager(hass, SimpleNamespace(entry_id="entry"))


def _path(hass):
    return Path(hass.config.path(".storage", "waha_whatsapp.correlated.entry"))


async def _execute(manager, callback, key="key"):
    return await manager.async_execute("direct_route", key, REQUEST, callback)


@pytest.mark.asyncio
async def test_real_ha_store_swallows_write_failure_but_transport_store_does_not(
    tmp_path,
    monkeypatch,
):
    hass = HomeAssistant(str(tmp_path))
    regular = Store(hass, 1, "comparison", private=True, atomic_writes=True)
    regular._async_write_data = AsyncMock(side_effect=file_util.WriteError("disk full"))
    # This is the real HA API behavior that cannot acknowledge send intent.
    await regular.async_save({"not_durable": True})
    assert not await asyncio.to_thread(Path(regular.path).exists)

    manager = _manager(hass)
    await manager.async_start()
    monkeypatch.setattr(
        file_util, "write_utf8_file_atomic", lambda *_args, **_kwargs: _raise_write()
    )
    send = AsyncMock(return_value="msg_should_not_send")
    outcome = await _execute(manager, send)
    assert outcome["status"] == "failed"
    assert outcome["error_code"] == "persistence_unavailable"
    assert manager.persistence_available is False
    send.assert_not_awaited()
    assert not _path(hass).exists()


def _raise_write():
    raise file_util.WriteError("disk full")


@pytest.mark.asyncio
async def test_real_ha_serialization_failure_blocks_intent(tmp_path, monkeypatch):
    hass = HomeAssistant(str(tmp_path))
    manager = _manager(hass)
    await manager.async_start()

    def fail_serialization(_data):
        raise SerializationError("cannot serialize")

    monkeypatch.setattr(manager._store, "_serialize", fail_serialization)
    callback = AsyncMock(return_value="msg_never")
    result = await _execute(manager, callback)
    assert result["error_code"] == "persistence_unavailable"
    callback.assert_not_awaited()
    assert not _path(hass).exists()


@pytest.mark.asyncio
async def test_failed_real_final_write_stays_unknown_and_replays_after_reload(
    tmp_path, monkeypatch
):
    hass = HomeAssistant(str(tmp_path))
    manager = _manager(hass)
    await manager.async_start()
    original = file_util.write_utf8_file_atomic
    writes = 0

    def fail_second(*args, **kwargs):
        nonlocal writes
        writes += 1
        if writes == 2:
            raise file_util.WriteError("disk full")
        return original(*args, **kwargs)

    monkeypatch.setattr(file_util, "write_utf8_file_atomic", fail_second)
    callback = AsyncMock(return_value="msg_provider_accepted")
    first = await _execute(manager, callback)
    assert first["status"] == "unknown"
    assert first["error_code"] == "outcome_unknown"
    durable = json.loads(_path(hass).read_bytes())
    assert next(iter(durable["data"]["records"].values()))["status"] == "pending"
    assert (await _execute(manager, callback))["replayed"]
    replacement = _manager(hass)
    await replacement.async_start()
    assert (await _execute(replacement, callback))["status"] == "unknown"
    callback.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("contents", [b"{corrupt-json", b"{}", b"[]", b""])
async def test_strict_load_preserves_corrupt_file_and_refuses_new_sends(
    tmp_path, contents
):
    hass = HomeAssistant(str(tmp_path))
    path = _path(hass)
    path.parent.mkdir(parents=True)
    path.write_bytes(contents)
    manager = _manager(hass)
    await manager.async_start()
    callback = AsyncMock(return_value="msg_never")
    assert (await _execute(manager, callback))[
        "error_code"
    ] == "persistence_unavailable"
    callback.assert_not_awaited()
    assert path.read_bytes() == contents
    assert not list(path.parent.glob("*.corrupt.*"))


@pytest.mark.asyncio
async def test_durable_store_is_private_standard_store_format_and_works_while_stopping(
    tmp_path,
):
    hass = HomeAssistant(str(tmp_path))
    manager = _manager(hass)
    await manager.async_start()
    hass.set_state(CoreState.stopping)
    callback = AsyncMock(return_value="msg_accepted")
    result = await _execute(manager, callback)
    assert result["status"] == "accepted"
    path = _path(hass)
    assert path.stat().st_mode & 0o777 == 0o600
    regular = Store(hass, 1, "waha_whatsapp.correlated.entry", private=True)
    saved = await regular.async_load()
    assert next(iter(saved["records"].values()))["status"] == "accepted"
    assert "Private content" not in path.read_text()
    assert manager._store._store._unsub_final_write_listener is None


@pytest.mark.asyncio
async def test_reload_drains_old_send_before_new_ledger_can_be_written(tmp_path):
    hass = HomeAssistant(str(tmp_path))
    old = _manager(hass)
    await old.async_start()
    entered = asyncio.Event()
    release = asyncio.Event()

    async def slow_send():
        entered.set()
        await release.wait()
        return "msg_old"

    send_a = asyncio.create_task(_execute(old, slow_send, "A"))
    await entered.wait()
    replacement = _manager(hass)
    start = asyncio.create_task(replacement.async_start())
    await asyncio.sleep(0)
    assert not start.done()
    callback = AsyncMock(return_value="msg_B")
    assert (await _execute(old, callback, "late"))[
        "error_code"
    ] == "channel_unavailable"
    callback.assert_not_awaited()
    release.set()
    assert (await send_a)["status"] == "accepted"
    await start
    assert (await _execute(replacement, callback, "B"))["status"] == "accepted"
    assert len(json.loads(_path(hass).read_bytes())["data"]["records"]) == 2
    third = _manager(hass)
    await third.async_start()
    assert (await _execute(third, callback, "B"))["replayed"]
    assert (await _execute(third, callback, "A"))["replayed"]
    callback.assert_awaited_once()


@pytest.mark.asyncio
async def test_cancelled_executor_write_cannot_outlive_runtime_handoff(
    tmp_path, monkeypatch
):
    hass = HomeAssistant(str(tmp_path))
    old = _manager(hass)
    await old.async_start()
    original = file_util.write_utf8_file_atomic
    entered = threading.Event()
    release = threading.Event()
    writes = 0

    def stall_final_write(*args, **kwargs):
        nonlocal writes
        writes += 1
        if writes == 2:
            entered.set()
            assert release.wait(5)
        return original(*args, **kwargs)

    monkeypatch.setattr(file_util, "write_utf8_file_atomic", stall_final_write)
    callback = AsyncMock(return_value="msg_old")
    send = asyncio.create_task(_execute(old, callback, "A"))
    assert await asyncio.to_thread(entered.wait, 3)
    replacement = _manager(hass)
    start = asyncio.create_task(replacement.async_start())
    try:
        send.cancel()
        await asyncio.sleep(0)
        send.cancel()
        await asyncio.sleep(0)
        assert not send.done()
        assert not start.done()
    finally:
        release.set()
    with pytest.raises(asyncio.CancelledError):
        await send
    await start
    assert (await _execute(replacement, callback, "A"))["replayed"]
    assert (await _execute(replacement, callback, "B"))["status"] == "accepted"
    third = _manager(hass)
    await third.async_start()
    assert (await _execute(third, callback, "B"))["replayed"]
    assert len(json.loads(_path(hass).read_bytes())["data"]["records"]) == 2
    assert callback.await_count == 2


@pytest.mark.asyncio
async def test_exact_physical_budget_admits_only_restartable_snapshots(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(correlated, "MAX_IDEMPOTENCY_STORAGE_BYTES", 4096)
    hass = HomeAssistant(str(tmp_path))
    manager = _manager(hass)
    await manager.async_start()
    callback = AsyncMock(return_value="\x00" * correlated.MAX_MESSAGE_ID_BYTES)
    accepted = []
    for index in range(20):
        key = f"key-{index}"
        result = await _execute(manager, callback, key)
        if result["status"] == "failed":
            assert result["error_code"] == "persistence_unavailable"
            break
        accepted.append(key)
        assert _path(hass).stat().st_size <= 4096
        assert manager.diagnostics["storage_bytes"] == _path(hass).stat().st_size
    else:
        pytest.fail("Small physical budget must eventually fence admission")
    assert accepted
    assert callback.await_count == len(accepted)
    replacement = _manager(hass)
    await replacement.async_start()
    assert replacement.persistence_available
    for key in accepted:
        result = await _execute(replacement, callback, key)
        assert result["replayed"]
        assert result["status"] == "accepted"
    assert callback.await_count == len(accepted)


@pytest.mark.asyncio
async def test_store_rejects_unserializable_values_before_touching_disk(tmp_path):
    hass = HomeAssistant(str(tmp_path))
    store = correlated.DurableStore(hass, 1, "serialization")
    with pytest.raises(TypeError):
        await store.async_save({"value": object()})
    assert not await asyncio.to_thread(Path(store._store.path).exists)
