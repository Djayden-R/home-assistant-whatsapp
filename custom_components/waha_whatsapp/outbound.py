"""Optional outbound observations, independent of any event consumer."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from contextlib import suppress
from copy import deepcopy
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from .api import (
    WahaAuthenticationError,
    WahaMessage,
    WahaRequestError,
    WahaSessionNotFoundError,
)
from .const import EVENT_WAHA_WHATSAPP
from .correlated import CorrelatedSendError
from .general_groups import GeneralGroupError
from .guest_manager import GuestGroupError

_OBSERVATION_TIMEOUT_SECONDS = 2
_ORIGINS = {"automation", "application", "human", "unknown"}


class OutboundObserver:
    """Observe one actual send attempt without changing its delivery result.

    Callers supply a route validated against configuration, never one inferred
    from the backend's returned phone/chat ID. Callback exceptions are re-raised
    unchanged; tracking and event bus failures are contained and sanitized.
    """

    def __init__(self, hass: Any, entry: Any, channel_registry: Any) -> None:
        self._hass = hass
        self._entry = entry
        self._registry = channel_registry
        self.observed_count = 0
        self.observation_failure_count = 0
        self.tracking_failure_count = 0

    @property
    def enabled(self) -> bool:
        return self._entry.options.get("outbound_observations_enabled") is True

    @property
    def health(self) -> str:
        if not self.enabled:
            return "unavailable"
        if (
            self.observation_failure_count
            or self.tracking_failure_count
            or getattr(self._registry, "persistence_available", True) is False
        ):
            return "degraded"
        return "ready"

    def snapshot(self) -> dict[str, Any]:
        """Expose counters only, with no messages, recipients, or exceptions."""
        return {
            "enabled": self.enabled,
            "health": self.health,
            "observed": self.observed_count,
            "observation_failures": self.observation_failure_count,
            "tracking_failures": self.tracking_failure_count,
        }

    async def async_send(
        self,
        conversation_id: str | None,
        conversation_type: str,
        content: dict[str, Any],
        async_send: Callable[[], Awaitable[WahaMessage]],
        *,
        origin: str = "unknown",
        client_request_id: str | None = None,
        content_redacted: bool = False,
    ) -> WahaMessage:
        """Send once and publish at most one normalized outcome observation."""
        try:
            result = await async_send()
        except (Exception, asyncio.CancelledError) as err:
            status = (
                "failed"
                if isinstance(
                    err,
                    (
                        WahaRequestError,
                        WahaAuthenticationError,
                        WahaSessionNotFoundError,
                        GeneralGroupError,
                        GuestGroupError,
                    ),
                )
                else "unknown"
            )
            if isinstance(err, CorrelatedSendError):
                status = err.status
            elif isinstance(err, WahaRequestError) and err.status >= 500:
                status = "unknown"
            await self._async_observe_safely(
                conversation_id,
                conversation_type,
                content,
                None,
                status,
                origin,
                client_request_id,
                content_redacted,
            )
            raise
        await self._async_observe_safely(
            conversation_id,
            conversation_type,
            content,
            result,
            (
                "accepted"
                if isinstance(result, WahaMessage) and result.outcome == "accepted"
                else "unknown"
            ),
            origin,
            client_request_id,
            content_redacted,
        )
        return result

    async def _async_observe_safely(
        self,
        conversation_id: str | None,
        conversation_type: str,
        content: dict[str, Any],
        result: WahaMessage | None,
        status: str,
        origin: str,
        client_request_id: str | None,
        content_redacted: bool,
    ) -> None:
        try:
            await self._async_observe(
                conversation_id,
                conversation_type,
                content,
                result,
                status,
                origin,
                client_request_id,
                content_redacted,
            )
        except Exception, asyncio.CancelledError:
            self.observation_failure_count += 1

    async def _async_observe(
        self,
        conversation_id: str | None,
        conversation_type: str,
        content: dict[str, Any],
        result: WahaMessage | None,
        status: str,
        origin: str,
        client_request_id: str | None,
        content_redacted: bool,
    ) -> None:
        if not conversation_id:
            if self.enabled:
                self.observation_failure_count += 1
            return
        message_id = None
        if isinstance(result, WahaMessage) and result.id is not None:
            try:
                async with asyncio.timeout(_OBSERVATION_TIMEOUT_SECONDS):
                    message_id = await self._registry.async_remember_message(
                        result.id, conversation_id
                    )
            except Exception, asyncio.CancelledError:
                self.tracking_failure_count += 1
        if not self.enabled:
            return
        # Build from an allowlist. A callback/provider object cannot leak extra
        # fields (raw IDs, request headers, action IDs) into the public event.
        message = {
            "id": message_id,
            "kind": content["kind"],
            "in_reply_to": content.get("in_reply_to"),
        }
        if not content_redacted:
            if "text" in content:
                message["text"] = content["text"]
            if content["kind"] == "poll":
                message["options"] = content["options"]
        event = {
            "schema_version": 1,
            "event_id": f"evt_{uuid4().hex}",
            "type": "message.outbound",
            "config_entry_id": self._entry.entry_id,
            "conversation_id": conversation_id,
            "conversation_type": conversation_type,
            "occurred_at": datetime.now(UTC).isoformat(),
            "origin": origin if origin in _ORIGINS else "unknown",
            "client_request_id": client_request_id,
            "status": status,
            "message": message,
        }
        if content_redacted:
            event["content_redacted"] = True
        self._hass.bus.async_fire(EVENT_WAHA_WHATSAPP, deepcopy(event))
        self.observed_count += 1


async def async_send_observed(
    entry: Any,
    conversation_id: str | None,
    conversation_type: str,
    content: dict[str, Any],
    async_send: Callable[[], Awaitable[WahaMessage]],
    **metadata: Any,
) -> WahaMessage:
    """Keep notify delivery working even if an observer itself is unavailable.

    Retain the actual callback's outcome if an observer fails after submission;
    never repeat an accepted or uncertain network attempt.
    """
    started = False
    result = None
    send_error = None
    observer = None

    async def send_once() -> WahaMessage:
        nonlocal started, result, send_error
        if not started:
            started = True
            try:
                result = await async_send()
            except (Exception, asyncio.CancelledError) as err:
                send_error = err
        if send_error is not None:
            raise send_error from None
        return result

    try:
        observer = entry.runtime_data.outbound_observer
        await observer.async_send(
            conversation_id, conversation_type, content, send_once, **metadata
        )
        if not started:
            with suppress(Exception):
                observer.observation_failure_count += 1
            return await send_once()
        if send_error is not None:
            raise send_error from None
        return result
    except (Exception, asyncio.CancelledError) as err:
        if send_error is not None:
            raise send_error from None
        with suppress(Exception):
            observer.observation_failure_count += 1
        if started:
            return result
        if isinstance(err, asyncio.CancelledError):
            raise
        # Legacy notify remains usable when runtime observation setup is absent.
        return await send_once()
