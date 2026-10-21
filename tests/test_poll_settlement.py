"""Timestamp and transaction regressions through actual poll service journeys."""

import asyncio
import hashlib
import hmac
import json
import math
import threading
from copy import deepcopy
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from ._loader import INTEGRATION_PATH, load_integration_module
from .test_application_poll_roundtrip import (
    OTHER,
    PHONE,
    SIGNING_KEY,
    environment,
    restart,
    selections,
    send,
    sending,
    vote,
    webhook,
)
from .test_channel_services import _functions
from .test_poll_channel import manager_module

polls = load_integration_module("polls")
correlated = load_integration_module("correlated")


async def public_receipts(env, *, conversation=None):
    """Invoke the production service body, including its loaded-entry selection."""
    _functions(
        INTEGRATION_PATH / "__init__.py",
        {"_async_handle_get_poll_selections"},
        env.handlers,
    )
    call = SimpleNamespace(
        hass=env.hass,
        data={
            "config_entry_id": env.entry.entry_id,
            "conversation_id": conversation or env.direct,
            "after_sequence": 0,
            "limit": 100,
        },
    )
    return await env.handlers["_async_handle_get_poll_selections"](call)


async def public_send(env):
    env.handlers["async_send_poll_to_conversation"] = (
        sending.async_send_poll_to_conversation
    )
    _functions(
        INTEGRATION_PATH / "__init__.py",
        {"_async_handle_send_poll_to_conversation"},
        env.handlers,
    )
    env.client._request.return_value = {"id": f"true_{PHONE}_POLLONE"}
    call = SimpleNamespace(
        hass=env.hass,
        data={
            "config_entry_id": env.entry.entry_id,
            "conversation_id": env.direct,
            "client_request_id": "request_ONE",
            "message": "Synthetic choice",
            "settle_seconds": 5,
            "actions": [
                {"action": "option_yes", "title": "Yes"},
                {"action": "option_no", "title": "No"},
            ],
        },
    )
    return await env.handlers["_async_handle_send_poll_to_conversation"](call)


@pytest.mark.parametrize(
    ("offset_ms", "iso_fraction"),
    [
        (125, ".125000"),
        (0.125, ".000125"),
    ],
    ids=["milliseconds", "fractional_milliseconds"],
)
async def test_gows_millisecond_vote_real_public_journey_survives_timer_and_restart(
    monkeypatch,
    offset_ms,
    iso_fraction,
):
    """Regression: 1.10.1 raises year overflow here after consuming the poll."""
    env = await environment(monkeypatch)
    assert env.clock.now == datetime(2030, 1, 2, 10, tzinfo=UTC).timestamp()
    assert (await public_send(env))["status"] == "accepted"
    timestamp_ms = env.clock.now * 1000 + offset_ms
    assert (await webhook(env, vote(env, timestamp=timestamp_ms))).status == 200
    assert not (await public_receipts(env))["events"]
    await webhook(env, vote(env, timestamp=timestamp_ms / 1000))
    assert env.manager.rejected_vote_reasons == {"stale_vote": 1}
    await env.clock.advance(5)
    [receipt] = (await public_receipts(env))["events"]
    assert receipt["vote_occurred_at"] == f"2030-01-02T10:00:00{iso_fraction}+00:00"
    assert receipt["occurred_at"] == "2030-01-02T10:00:05+00:00"
    assert receipt["poll"]["action_id"] == "option_yes"
    assert receipt["poll"]["selected_option"] is None
    assert selections(env) == [receipt]
    assert env.manager.pending_count == 0
    await restart(env)
    assert (await public_receipts(env))["events"] == [receipt]
    await webhook(env, vote(env, timestamp=timestamp_ms))
    await env.clock.advance(10)
    assert (await public_receipts(env))["events"] == [receipt]


async def test_fractional_boundary_public_votes_deduplicate_in_both_unit_orders(
    monkeypatch,
):
    raw_ms = 1893578400000.0054
    expected_seconds = 1893578400.000005
    receipts = []
    for forms in ((raw_ms, raw_ms / 1000), (raw_ms / 1000, raw_ms)):
        env = await environment(monkeypatch)
        assert (await public_send(env))["status"] == "accepted"
        await webhook(env, vote(env, timestamp=forms[0]))
        first_timer = env.manager._timers["POLLONE"]
        await webhook(env, vote(env, timestamp=forms[1]))
        assert env.manager.rejected_vote_reasons == {"stale_vote": 1}
        assert env.manager._timers["POLLONE"] is first_timer
        assert (
            env.manager._registry.pending("POLLONE").latest_vote_timestamp
            == expected_seconds
        )
        await env.clock.advance(5)
        [receipt] = (await public_receipts(env))["events"]
        assert receipt["vote_occurred_at"] == "2030-01-02T10:00:00.000005+00:00"
        assert receipt["event_id"] == env.registry.event_token(
            f"poll.selection_settled:POLLONE:{expected_seconds}"
        )
        assert selections(env) == [receipt]
        await restart(env)
        assert (await public_receipts(env))["events"] == [receipt]
        receipts.append(receipt)
    assert receipts[0] == receipts[1]


@pytest.mark.parametrize(
    "offset_ms", [0.0005, 0.0049, 0.0051, 0.0054, 0.0056, 0.125, 125]
)
def test_equivalent_numeric_units_share_idempotent_microsecond_rounding(offset_ms):
    raw_ms = 1893578400000 + offset_ms
    canonical = polls.normalize_vote_timestamp(raw_ms)
    assert canonical == polls.normalize_vote_timestamp(raw_ms / 1000)
    assert canonical == polls.normalize_vote_timestamp(canonical, unit="seconds")


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (0, 0.0),
        (123, 123.0),
        (123.125, 123.125),
        (1893578400.125, 1893578400.125),
        (1893578400125, 1893578400.125),
        (1893578400000.125, 1893578400.000125),
        (253402300800, 253402300.8),
        (polls.MAX_TIMESTAMP_SECONDS, polls.MAX_TIMESTAMP_SECONDS),
    ],
)
def test_numeric_timestamp_contract_has_explicit_units_and_microsecond_precision(
    value, expected
):
    assert polls.normalize_vote_timestamp(value) == expected


@pytest.mark.parametrize(
    "value",
    [
        True,
        False,
        None,
        "1893578400000",
        [],
        {},
        -1,
        math.nan,
        math.inf,
        -math.inf,
        10**400,
        polls.MAX_TIMESTAMP_MILLISECONDS + 1,
    ],
)
async def test_invalid_provider_timestamp_never_compares_persists_or_converts(
    monkeypatch, value
):
    env = await environment(monkeypatch)
    await send(env)
    incoming = vote(env)
    incoming["payload"]["vote"]["timestamp"] = value
    await webhook(env, incoming)
    await env.clock.advance(10)
    assert env.manager.rejected_vote_reasons == {"invalid_timestamp": 1}
    assert env.manager._registry.pending("POLLONE").latest_vote_timestamp is None
    assert not (await public_receipts(env))["events"]


async def test_old_pending_ms_load_normalizes_before_duplicate_ordering_and_correction(
    monkeypatch,
):
    env = await environment(monkeypatch)
    await send(env)
    await webhook(env, vote(env, timestamp=env.clock.now + 0.125))
    snapshot = deepcopy(env.manager._store.data)
    old = snapshot["polls"][0]
    old.pop("timestamp_unit")
    old["latest_vote_timestamp"] = (env.clock.now + 0.125) * 1000
    env.manager._store.data = snapshot
    await restart(env)
    pending = env.manager._registry.pending("POLLONE")
    assert pending.latest_vote_timestamp == env.clock.now + 0.125
    assert pending.as_dict()["timestamp_unit"] == "seconds"
    await webhook(env, vote(env, timestamp=(env.clock.now + 0.125) * 1000))
    await webhook(env, vote(env, timestamp=env.clock.now))
    assert env.manager.rejected_vote_reasons == {"stale_vote": 2}
    await webhook(
        env, vote(env, selected=["No"], timestamp=(env.clock.now + 0.25) * 1000)
    )
    await env.clock.advance(5)
    [receipt] = (await public_receipts(env))["events"]
    assert receipt["poll"]["action_id"] == "option_no"
    assert receipt["vote_occurred_at"].endswith("00.250000+00:00")


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("latest_vote_timestamp", math.nan),
        ("latest_vote_timestamp", True),
        ("latest_vote_timestamp", 10**400),
        ("latest_vote_timestamp", "bad"),
        ("commit_at", math.inf),
        ("commit_at", True),
        ("commit_at", -1),
        ("commit_at", polls.MAX_TIMESTAMP_SECONDS + 1),
    ],
)
async def test_unsafe_restored_vote_is_quarantined_without_losing_registered_poll(
    monkeypatch, field, value
):
    env = await environment(monkeypatch)
    await send(env)
    await webhook(env, vote(env))
    snapshot = deepcopy(env.manager._store.data)
    snapshot["polls"][0][field] = value
    env.manager._store.data = snapshot
    await restart(env)
    pending = env.manager._registry.pending("POLLONE")
    assert pending is not None
    assert pending.latest_vote_timestamp is None
    assert pending.selected_title is None
    assert pending.commit_at is None
    assert pending.options == {"Yes": "option_yes", "No": "option_no"}
    assert env.manager.diagnostics["invalid_pending_timestamps"] == 1
    assert not env.manager._timers
    await webhook(env, vote(env, timestamp=env.clock.now * 1000))
    await env.clock.advance(5)
    assert len((await public_receipts(env))["events"]) == 1


def test_marked_seconds_cannot_be_reinterpreted_and_float_datetime_boundary_is_safe():
    assert polls.normalize_vote_timestamp(1893578400000, unit="seconds") is None
    assert polls.normalize_vote_timestamp(253402300800, unit="seconds") is None
    result = polls.normalize_vote_timestamp(polls.MAX_TIMESTAMP_MILLISECONDS)
    assert result is not None
    assert datetime.fromtimestamp(result, UTC).year == 9999
    assert polls.normalize_vote_timestamp(123, unit="seconds") == 123


async def test_huge_json_integer_is_a_safe_authenticated_webhook_refusal(monkeypatch):
    env = await environment(monkeypatch)
    await send(env)
    raw = b'{"event":"poll.vote","payload":{"vote":{"timestamp":' + b"9" * 5000 + b"}}}"
    request = SimpleNamespace(
        content=SimpleNamespace(read=AsyncMock(return_value=raw)),
        headers={
            "X-Webhook-Hmac-Algorithm": "sha512",
            "X-Webhook-Hmac": hmac.new(
                SIGNING_KEY.encode(), raw, hashlib.sha512
            ).hexdigest(),
        },
    )
    unused = SimpleNamespace()
    result = await env.handlers["_async_handle_waha_webhook"](
        env.manager,
        unused,
        unused,
        unused,
        False,
        env.hass,
        "synthetic",
        request,
    )
    assert result.status == 400
    assert env.manager.pending_count == 1
    assert not (await public_receipts(env))["events"]


async def test_construction_failure_preserves_pending_and_bounded_native_recovery(
    monkeypatch,
):
    env = await environment(monkeypatch)
    await send(env)
    await webhook(env, vote(env))
    original = env.manager._settled_selection

    def failure(*_args):
        raise ValueError("synthetic construction failure")

    monkeypatch.setattr(env.manager, "_settled_selection", failure)
    await env.clock.advance(5)
    assert env.manager.pending_count == 1
    assert not (await public_receipts(env))["events"]
    assert env.manager.diagnostics["construction_failures"] == 1
    assert env.manager.diagnostics["scheduled_work"] == 1
    monkeypatch.setattr(env.manager, "_settled_selection", original)
    await env.clock.advance(1)
    assert len((await public_receipts(env))["events"]) == 1
    assert env.manager.diagnostics["recovery_pending"] == 0


async def test_failed_staging_and_retry_exhaustion_are_bounded_and_recoverable(
    monkeypatch,
):
    env = await environment(monkeypatch)
    await send(env)
    await webhook(env, vote(env))
    original = polls.PollRegistry.remember_selection

    def fail_staging(*_args):
        raise ValueError("synthetic staging failure")

    monkeypatch.setattr(polls.PollRegistry, "remember_selection", fail_staging)
    await env.clock.advance(5)
    for delay in manager_module.RECOVERY_DELAYS:
        await env.clock.advance(delay)
    assert env.manager.diagnostics["construction_failures"] == 4
    assert env.manager.diagnostics["retry_exhausted"] == 1
    assert env.manager.diagnostics["scheduled_work"] == 0
    assert env.manager.pending_count == 1
    await env.clock.advance(100)
    assert env.manager.diagnostics["construction_failures"] == 4
    monkeypatch.setattr(polls.PollRegistry, "remember_selection", original)
    await webhook(env, vote(env, selected=["No"], timestamp=env.clock.now))
    await env.clock.advance(5)
    assert (await public_receipts(env))["events"][0]["poll"]["action_id"] == "option_no"


async def test_blocked_failed_write_hides_receipt_and_cannot_roll_back_newer_answer(
    monkeypatch,
):
    env = await environment(monkeypatch)
    await send(env)
    env.entry.subentries["other"] = SimpleNamespace(
        subentry_id="other",
        subentry_type="recipient",
        data={"recipient": OTHER.partition("@")[0]},
    )
    other_route = env.registry._contact(env.entry.subentries["other"]).conversation_id
    env.client._request.return_value = {"id": f"true_{OTHER}_POLLTWO"}
    assert (
        await sending.async_send_poll_to_conversation(
            env.entry,
            conversation_id=other_route,
            client_request_id="request_TWO",
            message="Synthetic second choice",
            settle_seconds=5,
            actions=[
                {"action": "option_yes", "title": "Yes"},
                {"action": "option_no", "title": "No"},
            ],
        )
    )["status"] == "accepted"
    await webhook(env, vote(env))
    entered, release = asyncio.Event(), asyncio.Event()
    original = env.manager._store.async_save
    once = True

    async def blocked(snapshot):
        nonlocal once
        if snapshot["selections"] and once:
            once = False
            entered.set()
            await release.wait()
            raise OSError("synthetic blocked disk failure")
        await original(snapshot)

    monkeypatch.setattr(env.manager._store, "async_save", blocked)
    settling = asyncio.create_task(env.clock.advance(5))
    await entered.wait()
    assert not (await public_receipts(env))["events"]
    assert env.manager.pending_count == 2
    newer = asyncio.create_task(
        webhook(env, vote(env, selected=["No"], timestamp=env.clock.now))
    )
    second = asyncio.create_task(
        webhook(
            env,
            vote(
                env,
                suffix="TWO",
                sender=OTHER,
                destination=OTHER,
                timestamp=env.clock.now,
            ),
        )
    )
    await asyncio.sleep(0)
    assert not newer.done()
    assert not second.done()
    release.set()
    await asyncio.gather(settling, newer, second)
    assert env.manager._registry.pending("POLLONE").selected_title == "No"
    assert env.manager._registry.pending("POLLTWO").selected_title == "Yes"
    current_timer = env.manager._timers["POLLONE"]
    # Deliver an already queued callback from the failed old selection anyway.
    stale = next(timer for timer in env.clock.timers if timer.cancelled)
    stale.callback(*stale.args)
    assert env.manager._timers["POLLONE"] is current_timer
    await env.manager._async_commit("POLLONE", env.clock.now - 5)
    assert not (await public_receipts(env))["events"]
    await env.clock.advance(5)
    receipts = [
        (await public_receipts(env))["events"][0],
        (await public_receipts(env, conversation=other_route))["events"][0],
    ]
    assert [row["poll"]["action_id"] for row in receipts] == ["option_no", "option_yes"]
    assert [row["selection_sequence"] for row in receipts] == [1, 2]


@pytest.mark.parametrize("failure", ["bus", "context"])
async def test_after_durable_commit_public_replay_survives_publication_failure(
    monkeypatch, failure
):
    env = await environment(monkeypatch)
    await send(env)
    await webhook(env, vote(env))
    if failure == "bus":

        def broken_bus(*_args, **_kwargs):
            raise ValueError("synthetic publication failure")

        monkeypatch.setattr(env.hass.bus, "async_fire", broken_bus)
    else:
        monkeypatch.setattr(
            env.manager,
            "_async_person_context",
            AsyncMock(side_effect=ValueError("synthetic context failure")),
        )
    await env.clock.advance(5)
    [receipt] = (await public_receipts(env))["events"]
    assert env.manager.diagnostics["publication_failures"] == 1
    await restart(env)
    assert (await public_receipts(env))["events"] == [receipt]
    await env.clock.advance(100)
    assert (await public_receipts(env))["events"] == [receipt]


async def disk_environment(monkeypatch, tmp_path):
    pytest.importorskip("homeassistant")
    from homeassistant.core import HomeAssistant
    from homeassistant.util import file as file_util

    env = await environment(monkeypatch)
    env.disk_hass = HomeAssistant(str(tmp_path))
    env.manager._store = correlated.DurableStore(env.disk_hass, 1, "synthetic_polls")
    env.disk_path = Path(env.manager._store._store.path)
    env.file_util = file_util
    return env


async def legacy_send(env):
    env.client._request.return_value = {"id": f"true_{PHONE}_POLLONE"}
    result = await env.client.async_send_poll(
        PHONE, "Synthetic choice", ["Yes", "No action"]
    )
    await env.manager.async_register_poll(
        result.id,
        result.chat_id,
        polls.build_poll_options(
            [{"action": "legacy_option", "title": "Yes"}], "No action"
        ),
        5,
    )


@pytest.mark.parametrize("legacy", [False, True])
async def test_real_disk_failure_retains_vote_and_retries_before_publication(
    monkeypatch,
    tmp_path,
    legacy,
):
    env = await disk_environment(monkeypatch, tmp_path)
    await (legacy_send(env) if legacy else send(env))
    await webhook(env, vote(env))
    original = env.file_util.write_utf8_file_atomic

    def failing_disk(*_args, **_kwargs):
        raise env.file_util.WriteError("synthetic disk failure")

    monkeypatch.setattr(env.file_util, "write_utf8_file_atomic", failing_disk)
    await env.clock.advance(5)
    on_disk = json.loads(env.disk_path.read_text())["data"]
    assert len(on_disk["polls"]) == 1
    assert not on_disk["selections"]
    assert not (await public_receipts(env))["events"]
    assert env.manager.pending_count == 1
    assert env.manager.diagnostics["persistence_failures"] == 1
    monkeypatch.setattr(env.file_util, "write_utf8_file_atomic", original)
    await env.clock.advance(1)
    if legacy:
        assert not (await public_receipts(env))["events"]
        assert [name for name, _ in env.hass.bus.events].count(
            "mobile_app_notification_action"
        ) == 1
        await restart(env)
        await env.clock.advance(100)
        assert [name for name, _ in env.hass.bus.events].count(
            "mobile_app_notification_action"
        ) == 1
    else:
        [receipt] = (await public_receipts(env))["events"]
        assert (
            json.loads(env.disk_path.read_text())["data"]["selections"][0]["payload"]
            == receipt
        )


@pytest.mark.parametrize("legacy", [False, True])
async def test_cancelled_disk_write_promotes_state_and_drains_replacement(
    monkeypatch, tmp_path, legacy
):
    env = await disk_environment(monkeypatch, tmp_path)
    if legacy:
        await legacy_send(env)
    else:
        await send(env)
    await webhook(env, vote(env))
    original = env.file_util.write_utf8_file_atomic
    written, release = threading.Event(), threading.Event()

    def blocked_ack(*args, **kwargs):
        original(*args, **kwargs)
        written.set()
        assert release.wait(5)

    monkeypatch.setattr(env.file_util, "write_utf8_file_atomic", blocked_ack)
    env.clock.now += 5
    old = env.manager
    committing = asyncio.create_task(old._async_commit("POLLONE", env.clock.now - 5))
    assert await asyncio.to_thread(written.wait, 5)
    assert not (await public_receipts(env))["events"]
    committing.cancel()
    replacement = manager_module.WahaPollManager(
        env.hass,
        env.entry,
        env.client,
        SIGNING_KEY,
        env.registry,
        store=correlated.DurableStore(env.disk_hass, 1, "synthetic_polls"),
    )
    starting = asyncio.create_task(replacement.async_start())
    await asyncio.sleep(0)
    assert not starting.done()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await committing
    await starting
    env.entry.runtime_data.poll_manager = env.manager = replacement
    if legacy:
        assert [name for name, _ in env.hass.bus.events].count(
            "mobile_app_notification_action"
        ) == 1
        assert not (await public_receipts(env))["events"]
    else:
        assert len((await public_receipts(env))["events"]) == 1
        assert not selections(env)
    assert old._stopping
    assert replacement.pending_count == 0
    await env.clock.advance(100)
    assert replacement.pending_count == 0


async def test_public_send_drains_and_registers_before_actual_unload_then_restores(
    monkeypatch, tmp_path
):
    env = await disk_environment(monkeypatch, tmp_path)
    entry_ledger = correlated.CorrelatedSendManager(
        env.hass,
        env.entry,
        store=correlated.DurableStore(env.disk_hass, 1, "synthetic_send_ledger"),
    )
    await entry_ledger.async_start()
    env.entry.runtime_data.correlated_sends = entry_ledger
    entered, release = asyncio.Event(), asyncio.Event()

    async def provider(*_args, **_kwargs):
        entered.set()
        await release.wait()
        return {"id": f"true_{PHONE}_POLLONE"}

    env.client._request.side_effect = provider
    sending_task = asyncio.create_task(public_send(env))
    await entered.wait()
    env.hass.config_entries.async_unload_platforms = AsyncMock(return_value=True)
    unloading = asyncio.create_task(
        env.handlers["async_unload_entry"](env.hass, env.entry)
    )
    await asyncio.sleep(0)
    assert not unloading.done()
    assert not env.manager._stopping
    release.set()
    assert (await sending_task)["status"] == "accepted"
    assert await unloading
    on_disk = json.loads(env.disk_path.read_text())["data"]
    assert len(on_disk["polls"]) == 1
    assert on_disk["polls"][0]["origin"] == "application"
    assert env.manager._stopping
    assert not env.manager._timers
    channel_module = load_integration_module("channel_registry")
    channel = channel_module.ChannelRegistry(
        env.hass, env.entry, env.client, store=env.store
    )
    await channel.async_start()
    env.entry.runtime_data.channel_registry = env.registry = channel
    replacement = manager_module.WahaPollManager(
        env.hass,
        env.entry,
        env.client,
        SIGNING_KEY,
        channel,
        store=correlated.DurableStore(env.disk_hass, 1, "synthetic_polls"),
    )
    await replacement.async_start()
    env.entry.runtime_data.poll_manager = env.manager = replacement
    await webhook(env, vote(env, timestamp=env.clock.now * 1000))
    await env.clock.advance(5)
    assert len((await public_receipts(env))["events"]) == 1


@pytest.mark.parametrize("failure", ["construction", "context", "bus"])
async def test_legacy_observation_failures_never_replay_or_suppress_durable_action(
    monkeypatch, failure
):
    env = await environment(monkeypatch)
    options = polls.build_poll_options(
        [{"action": "legacy_option", "title": "Yes"}], "No action"
    )
    await env.manager.async_register_poll(f"true_{PHONE}_POLLONE", PHONE, options, 5)
    await webhook(env, vote(env))
    if failure == "construction":

        def broken(*_args):
            raise ValueError("synthetic observation failure")

        monkeypatch.setattr(env.manager, "_settled_selection", broken)
    elif failure == "context":
        monkeypatch.setattr(
            env.manager,
            "_async_person_context",
            AsyncMock(side_effect=ValueError("synthetic context failure")),
        )
    else:
        original = env.hass.bus.async_fire

        def broken_bus(name, payload, **kwargs):
            if name == "waha_whatsapp_event":
                raise ValueError("synthetic observation failure")
            original(name, payload, **kwargs)

        monkeypatch.setattr(env.hass.bus, "async_fire", broken_bus)
    await env.clock.advance(5)
    assert [name for name, _ in env.hass.bus.events].count(
        "mobile_app_notification_action"
    ) == 1
    assert not (await public_receipts(env))["events"]
    await restart(env)
    await env.clock.advance(100)
    assert [name for name, _ in env.hass.bus.events].count(
        "mobile_app_notification_action"
    ) == 1


async def test_revoked_route_keeps_application_work_pending_without_exposing_receipt(
    monkeypatch,
):
    env = await environment(monkeypatch)
    await send(env)
    await webhook(env, vote(env))
    original = env.entry.subentries["contact"].data
    env.entry.subentries["contact"].data = {"recipient": "18888888888"}
    await env.clock.advance(5)
    assert not (await public_receipts(env))["events"]
    assert not selections(env)
    assert env.manager.pending_count == 1
    env.entry.subentries["contact"].data = original
    await env.clock.advance(1)
    assert len((await public_receipts(env))["events"]) == 1


async def test_failed_vote_write_keeps_answer_and_correction_replaces_recovery(
    monkeypatch,
):
    env = await environment(monkeypatch)
    await send(env)
    original = env.manager._store.async_save
    fail_once = True

    async def fail(snapshot):
        nonlocal fail_once
        if fail_once:
            fail_once = False
            raise OSError("synthetic vote write failure")
        await original(snapshot)

    monkeypatch.setattr(env.manager._store, "async_save", fail)
    assert (await webhook(env, vote(env))).status == 200
    assert env.manager._registry.pending("POLLONE").selected_title == "Yes"
    assert not (await public_receipts(env))["events"]
    assert env.manager.diagnostics["recovery_pending"] == 1
    stale_timer = env.clock.timers[-1]
    await webhook(env, vote(env, selected=["No"], timestamp=env.clock.now + 1))
    current = env.manager._timers["POLLONE"]
    stale_timer.callback(*stale_timer.args)
    assert env.manager._timers["POLLONE"] is current
    await env.clock.advance(5)
    assert (await public_receipts(env))["events"][0]["poll"]["action_id"] == "option_no"


async def test_cancelled_vote_write_finishes_persistence_and_schedules_owned_settlement(
    monkeypatch,
):
    env = await environment(monkeypatch)
    await send(env)
    entered, release = asyncio.Event(), asyncio.Event()
    original = env.manager._store.async_save

    async def blocked(snapshot):
        entered.set()
        await release.wait()
        await original(snapshot)

    monkeypatch.setattr(env.manager._store, "async_save", blocked)
    receiving = asyncio.create_task(webhook(env, vote(env)))
    await entered.wait()
    receiving.cancel()
    await asyncio.sleep(0)
    assert not receiving.done()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await receiving
    assert env.manager._registry.pending("POLLONE").selected_title == "Yes"
    await env.clock.advance(5)
    assert len((await public_receipts(env))["events"]) == 1


async def test_recovery_slot_cap_retains_unscheduled_work_for_fresh_vote(monkeypatch):
    env = await environment(monkeypatch)
    await send(env)
    await send(env, suffix="TWO")
    await webhook(env, vote(env))
    await webhook(env, vote(env, suffix="TWO"))
    monkeypatch.setattr(manager_module, "MAX_RECOVERY_POLLS", 1)
    original = env.manager._settled_selection

    def broken(*_args):
        raise ValueError("synthetic preparation failure")

    monkeypatch.setattr(env.manager, "_settled_selection", broken)
    await env.clock.advance(5)
    assert env.manager.pending_count == 2
    assert env.manager.diagnostics["recovery_pending"] == 1
    assert env.manager.diagnostics["scheduled_work"] == 1
    assert env.manager.diagnostics["retry_exhausted"] == 1
    monkeypatch.setattr(env.manager, "_settled_selection", original)
    await env.clock.advance(1)
    assert env.manager.pending_count == 1
    await webhook(
        env, vote(env, suffix="TWO", selected=["No"], timestamp=env.clock.now)
    )
    await env.clock.advance(5)
    assert [
        row["poll"]["action_id"] for row in (await public_receipts(env))["events"]
    ] == ["option_yes", "option_no"]


@pytest.mark.parametrize("legacy", [False, True])
async def test_cancelled_context_lookup_preserves_durable_outcome_without_replay(
    monkeypatch, legacy
):
    env = await environment(monkeypatch)
    await (legacy_send(env) if legacy else send(env))
    await webhook(env, vote(env))
    monkeypatch.setattr(
        env.manager,
        "_async_person_context",
        AsyncMock(side_effect=asyncio.CancelledError),
    )
    with pytest.raises(asyncio.CancelledError):
        await env.clock.advance(5)
    assert env.manager.pending_count == 0
    assert env.manager.diagnostics["publication_failures"] == 1
    actions = [name for name, _ in env.hass.bus.events].count(
        "mobile_app_notification_action"
    )
    assert actions == int(legacy)
    receipts = (await public_receipts(env))["events"]
    assert len(receipts) == int(not legacy)
    # Remove the cancelled synthetic callback task retained by the fake clock.
    env.clock.tasks.clear()
    await restart(env)
    await env.clock.advance(100)
    assert (await public_receipts(env))["events"] == receipts
    assert [name for name, _ in env.hass.bus.events].count(
        "mobile_app_notification_action"
    ) == actions
