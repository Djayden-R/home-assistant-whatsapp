"""Durable keyed sends fence duplicates across uncertainty and restart."""

import asyncio
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from ._loader import load_integration_module

correlated = load_integration_module("correlated")
api = load_integration_module("api")
REQUEST = {
    "message": "A private rendered message",
    "reply_to_message_id": "msg_quote",
    "origin": "application",
}


class FakeStore:
    """Atomic storage with explicit load/save failure injection."""

    def __init__(self, data=None):
        self.data = deepcopy(data)
        self.fail_load = False
        self.fail_on_save = None
        self.save_count = 0

    async def async_load(self):
        if self.fail_load:
            raise OSError("Private backend details")
        return deepcopy(self.data)

    async def async_save(self, data):
        self.save_count += 1
        if self.save_count == self.fail_on_save:
            raise OSError("Private backend details")
        self.data = deepcopy(data)


def manager(store=None, entry_id="entry-a"):
    return correlated.CorrelatedSendManager(
        SimpleNamespace(),
        SimpleNamespace(entry_id=entry_id),
        store=store or FakeStore(),
    )


async def execute(instance, send, *, key="key", conversation="direct_a", request=None):
    return await instance.async_execute(conversation, key, request or REQUEST, send)


@pytest.mark.asyncio
async def test_intent_persisted_before_io_and_duplicate_replays_saved_result():
    store = FakeStore()
    instance = manager(store)
    await instance.async_start()

    async def send():
        assert next(iter(store.data["records"].values()))["status"] == "pending"
        return "msg_opaque"

    callback = AsyncMock(side_effect=send)
    first = await execute(instance, callback)
    assert first == {
        "conversation_id": "direct_a",
        "client_request_id": "key",
        "status": "accepted",
        "message_id": "msg_opaque",
        "replayed": False,
    }
    duplicate = await execute(instance, callback)
    assert duplicate == {**first, "replayed": True}
    callback.assert_awaited_once()
    assert REQUEST["message"] not in str(store.data)
    assert "msg_quote" not in str(store.data)
    assert "direct_a" not in str(store.data)
    assert "key" not in str(store.data)
    assert "direct_a" not in str(instance.diagnostics)


@pytest.mark.asyncio
async def test_concurrent_duplicate_returns_pending_without_waiting_for_io():
    instance = manager()
    await instance.async_start()
    entered = asyncio.Event()
    release = asyncio.Event()

    async def send():
        entered.set()
        await release.wait()
        return "msg_accepted"

    callback = AsyncMock(side_effect=send)
    first = asyncio.create_task(execute(instance, callback))
    await entered.wait()
    duplicate = await execute(instance, callback)
    assert duplicate["status"] == "pending"
    assert duplicate["replayed"] is True
    callback.assert_awaited_once()
    release.set()
    assert (await first)["status"] == "accepted"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("message", "different"),
        ("reply_to_message_id", "msg_other"),
        ("origin", "human"),
    ],
)
async def test_conflict_never_submits_again(field, value):
    instance = manager()
    await instance.async_start()
    callback = AsyncMock(return_value="msg_one")
    await execute(instance, callback)
    result = await execute(instance, callback, request={**REQUEST, field: value})
    assert result["status"] == "failed"
    assert result["error_code"] == "request_conflict"
    callback.assert_awaited_once()


@pytest.mark.asyncio
async def test_request_scope_is_entry_and_conversation_and_canonical_order():
    instance = manager()
    await instance.async_start()
    callback = AsyncMock(return_value=None)
    assert (await execute(instance, callback))["status"] == "accepted"
    assert (await execute(instance, callback, request=dict(reversed(REQUEST.items()))))[
        "replayed"
    ]
    await execute(instance, callback, conversation="direct_b")
    second_entry = manager(entry_id="entry-b")
    await second_entry.async_start()
    await execute(second_entry, callback)
    assert callback.await_count == 3
    # Missing optional values normalize to their canonical defaults.
    plain = {"message": "hello"}
    await execute(instance, callback, key="defaults", request=plain)
    assert (
        await execute(
            instance,
            callback,
            key="defaults",
            request={
                **plain,
                "reply_to_message_id": None,
                "origin": "unknown",
            },
        )
    )["replayed"]


@pytest.mark.asyncio
async def test_accepted_without_quote_tracking_is_still_accepted():
    instance = manager()
    await instance.async_start()
    callback = AsyncMock(return_value={"message_id": None})
    assert (await execute(instance, callback))["status"] == "accepted"
    assert (await execute(instance, callback))["replayed"]
    callback.assert_awaited_once()


@pytest.mark.asyncio
async def test_restart_pending_becomes_unknown_and_cannot_resend():
    store = FakeStore()
    first = manager(store)
    await first.async_start()
    entered = asyncio.Event()
    release = asyncio.Event()

    async def send():
        entered.set()
        await release.wait()
        return "msg_first"

    task = asyncio.create_task(execute(first, send))
    await entered.wait()
    restarted_store = FakeStore(store.data)
    restarted = manager(restarted_store)
    await restarted.async_start()
    callback = AsyncMock(return_value="msg_duplicate")
    result = await execute(restarted, callback)
    assert result["status"] == "unknown"
    assert result["error_code"] == "outcome_unknown"
    assert result["replayed"] is True
    callback.assert_not_awaited()
    assert next(iter(restarted_store.data["records"].values()))["status"] == "unknown"
    assert restarted.diagnostics["health"] == "degraded"
    release.set()
    await task


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["load", "intent", "malformed", "oversized"])
async def test_unavailable_storage_blocks_before_network(kind, monkeypatch):
    store = FakeStore()
    if kind == "load":
        store.fail_load = True
    elif kind == "intent":
        store.fail_on_save = 1
    elif kind == "malformed":
        store.data = {"records": {"broken": {"status": "pending"}}}
    elif kind == "oversized":
        monkeypatch.setattr(correlated, "MAX_IDEMPOTENCY_STORAGE_BYTES", 64)
    instance = manager(store)
    await instance.async_start()
    callback = AsyncMock(return_value="msg_bad")
    result = await execute(instance, callback)
    assert result["status"] == "failed"
    assert result["error_code"] == "persistence_unavailable"
    callback.assert_not_awaited()
    if kind != "oversized":
        assert not instance.capabilities["correlated_sends"]


@pytest.mark.asyncio
async def test_accepted_outcome_save_failure_is_unknown_in_process_and_after_restart():
    store = FakeStore()
    store.fail_on_save = 2
    instance = manager(store)
    await instance.async_start()
    callback = AsyncMock(return_value="msg_provider_accepted")
    first = await execute(instance, callback)
    assert first["status"] == "unknown"
    assert first["error_code"] == "outcome_unknown"
    assert first["message_id"] is None
    assert not instance.persistence_available
    assert (await execute(instance, callback))["replayed"]
    # Failing persistence never permits another new request to hit the wire.
    assert (await execute(instance, callback, key="other"))[
        "error_code"
    ] == "persistence_unavailable"
    restarted = manager(store)
    await restarted.async_start()
    assert (await execute(restarted, callback))["status"] == "unknown"
    callback.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("error", "status", "code"),
    [
        (api.WahaRequestError("secret", 429), "failed", "rate_limited"),
        (api.WahaRequestError("secret", 400), "failed", "transport_failure"),
        (api.WahaRequestError("secret", 503), "unknown", "outcome_unknown"),
        (api.WahaConnectionError("secret"), "unknown", "outcome_unknown"),
        (api.WahaResponseError("secret"), "unknown", "outcome_unknown"),
        (api.WahaAuthenticationError("secret"), "failed", "channel_unavailable"),
        (api.WahaSessionNotFoundError("secret"), "failed", "channel_unavailable"),
        (ValueError("secret"), "unknown", "outcome_unknown"),
        (
            correlated.CorrelatedSendError("invalid_reply_target"),
            "failed",
            "invalid_reply_target",
        ),
        (
            correlated.CorrelatedSendError("invalid_conversation"),
            "failed",
            "invalid_conversation",
        ),
    ],
)
async def test_safe_error_classification_is_persisted_without_provider_detail(
    error, status, code
):
    store = FakeStore()
    instance = manager(store)
    await instance.async_start()
    callback = AsyncMock(side_effect=error)
    first = await execute(instance, callback)
    assert first["status"] == status
    assert first["error_code"] == code
    assert "secret" not in str(first) + str(store.data) + str(instance.diagnostics)
    assert (await execute(instance, callback))["replayed"]
    callback.assert_awaited_once()


@pytest.mark.asyncio
async def test_cancellation_is_unknown_without_automatic_retry():
    instance = manager()
    await instance.async_start()
    entered = asyncio.Event()

    async def send():
        entered.set()
        await asyncio.Event().wait()

    task = asyncio.create_task(execute(instance, send))
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    callback = AsyncMock(return_value="never")
    result = await execute(instance, callback)
    assert result["status"] == "unknown"
    callback.assert_not_awaited()


@pytest.mark.asyncio
async def test_retention_expires_settled_but_never_unresolved_records(monkeypatch):
    now = [1_000_000.0]
    monkeypatch.setattr(correlated.time, "time", lambda: now[0])
    monkeypatch.setattr(correlated, "MAX_IDEMPOTENCY_RECORDS", 2)
    store = FakeStore()
    instance = manager(store)
    await instance.async_start()
    accepted = AsyncMock(return_value="msg_yes")
    unknown = AsyncMock(side_effect=api.WahaConnectionError("ambiguous"))
    await execute(instance, accepted, key="settled")
    await execute(instance, unknown, key="unresolved")
    blocked = AsyncMock(return_value="msg_new")
    assert (await execute(instance, blocked, key="extra"))[
        "error_code"
    ] == "persistence_unavailable"
    blocked.assert_not_awaited()
    assert instance.diagnostics["capacity_exhausted"]
    now[0] += correlated.IDEMPOTENCY_RETENTION_SECONDS + 1
    assert (await execute(instance, unknown, key="unresolved"))["status"] == "unknown"
    assert (await execute(instance, accepted, key="settled"))["replayed"] is False
    assert accepted.await_count == 2
    unknown.assert_awaited_once()
    assert len(store.data["records"]) == 2
    restarted = manager(store)
    await restarted.async_start()
    assert (await execute(restarted, unknown, key="unresolved"))["status"] == "unknown"
    unknown.assert_awaited_once()


@pytest.mark.asyncio
async def test_byte_capacity_reserves_worst_case_result_and_keeps_unknown(monkeypatch):
    store = FakeStore()
    instance = manager(store)
    await instance.async_start()
    monkeypatch.setattr(correlated, "MAX_IDEMPOTENCY_STORAGE_BYTES", 2000)
    callback = AsyncMock(return_value="\x00" * correlated.MAX_MESSAGE_ID_BYTES)
    assert (await execute(instance, callback))["status"] == "accepted"
    assert instance.diagnostics["storage_bytes"] <= 2000
    assert (await execute(instance, callback, key="extra"))[
        "error_code"
    ] == "persistence_unavailable"
    callback.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("key", ["", "x" * 129, None, 123])
async def test_invalid_keys_never_call_network(key):
    instance = manager()
    await instance.async_start()
    callback = AsyncMock()
    with pytest.raises(ValueError, match="client request"):
        await execute(instance, callback, key=key)
    callback.assert_not_awaited()


@pytest.mark.asyncio
async def test_invalid_or_raw_callback_results_are_uncertain():
    for value in (object(), {"status": "wrong"}, "x" * 257):
        instance = manager()
        await instance.async_start()
        callback = AsyncMock(return_value=value)
        assert (await execute(instance, callback))["status"] == "unknown"
        assert (await execute(instance, callback))["replayed"]
        callback.assert_awaited_once()


@pytest.mark.asyncio
async def test_start_is_once_and_accepted_replays_after_restart():
    store = FakeStore()
    instance = manager(store)
    await instance.async_start()
    callback = AsyncMock(return_value="msg_known")
    await execute(instance, callback)
    store.fail_load = True
    await instance.async_start()
    assert instance.persistence_available
    store.fail_load = False
    restarted = manager(store)
    await restarted.async_start()
    replay = await execute(restarted, callback)
    assert replay["status"] == "accepted"
    assert replay["message_id"] == "msg_known"
    callback.assert_awaited_once()


@pytest.mark.asyncio
async def test_owns_request_is_read_only_scoped_and_honors_settled_retention(
    monkeypatch,
):
    now = [1_000_000.0]
    monkeypatch.setattr(correlated.time, "time", lambda: now[0])
    store = FakeStore()
    instance = manager(store)
    await instance.async_start()
    callback = AsyncMock(return_value="msg_known")
    await execute(instance, callback, key="accepted")
    callback.side_effect = api.WahaConnectionError("uncertain")
    await execute(instance, callback, key="unknown")
    before = deepcopy(store.data)
    save_count = store.save_count
    assert instance.owns_request("direct_a", "accepted")
    assert instance.owns_request("direct_a", "unknown")
    assert not instance.owns_request("direct_b", "accepted")
    assert not instance.owns_request("direct_a", "different")
    assert not manager(entry_id="entry-b").owns_request("direct_a", "accepted")
    assert not instance.owns_request("direct_a", None)
    now[0] += correlated.IDEMPOTENCY_RETENTION_SECONDS + 1
    assert not instance.owns_request("direct_a", "accepted")
    assert instance.owns_request("direct_a", "unknown")
    assert store.data == before
    assert store.save_count == save_count
    assert instance.diagnostics["records"] == 2


@pytest.mark.asyncio
async def test_cancelled_stop_keeps_fence_and_replacement_waits_for_drain():
    hass = SimpleNamespace(data={})
    entry = SimpleNamespace(entry_id="entry-a")
    store = FakeStore()
    old = correlated.CorrelatedSendManager(hass, entry, store=store)
    await old.async_start()
    entered = asyncio.Event()
    release = asyncio.Event()

    async def slow_send():
        entered.set()
        await release.wait()
        return "msg_accepted"

    admitted = asyncio.create_task(execute(old, slow_send))
    await entered.wait()
    stop = asyncio.create_task(old.async_stop())
    await asyncio.sleep(0)
    stop.cancel()
    with pytest.raises(asyncio.CancelledError):
        await stop
    replacement = correlated.CorrelatedSendManager(hass, entry, store=store)
    start = asyncio.create_task(replacement.async_start())
    await asyncio.sleep(0)
    callback = AsyncMock(return_value="msg_not_sent")
    try:
        assert not start.done()
        assert (await execute(old, callback, key="late"))[
            "error_code"
        ] == "channel_unavailable"
        assert (await execute(old, callback))["status"] == "pending"
        callback.assert_not_awaited()
    finally:
        release.set()
    await admitted
    await start
    assert (await execute(replacement, callback))["status"] == "accepted"
    assert (await execute(old, callback))["replayed"]
    callback.assert_not_awaited()
