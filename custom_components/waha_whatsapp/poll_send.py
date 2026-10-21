"""Keyed application polls: communication and correlation only."""

from __future__ import annotations

import math
from typing import Any

from .correlated import CorrelatedSendError
from .outbound import async_send_observed
from .polls import PollValidationError, build_poll_options


async def async_send_poll_to_conversation(
    entry: Any,
    *,
    conversation_id: str,
    client_request_id: str,
    message: str,
    actions: list[dict[str, str]],
    title: str | None = None,
    settle_seconds: float = 0,
    content_redacted: bool = False,
) -> dict[str, Any]:
    """Send one immutable poll to an existing, unambiguous direct contact.

    Application selections emit the authenticated channel event; they never
    invoke the legacy mobile notification-action bus. Action IDs are opaque
    consumer data and are not interpreted here.
    """
    if (
        not isinstance(conversation_id, str)
        or not 1 <= len(conversation_id) <= 256
        or not isinstance(client_request_id, str)
        or not 1 <= len(client_request_id) <= 128
        or not isinstance(message, str)
        or not 1 <= len(message) <= 4096
        or (title is not None and (not isinstance(title, str) or len(title) > 128))
        or type(content_redacted) is not bool
        or isinstance(settle_seconds, bool)
        or not isinstance(settle_seconds, int | float)
        or not math.isfinite(settle_seconds)
        or not 0 <= settle_seconds <= 30
        or not isinstance(actions, list)
        or not 2 <= len(actions) <= 12
    ):
        raise CorrelatedSendError("invalid_request")
    for item in actions:
        if (
            not isinstance(item, dict)
            or set(item) != {"action", "title"}
            or any(not isinstance(item[k], str) or not item[k].strip() for k in item)
            or len(item["action"]) > 128
            or len(item["title"]) > 256
            or any("\x00" in item[k] for k in item)
        ):
            raise CorrelatedSendError("invalid_request")
    try:
        # With two or more explicit actions the legacy converter adds no
        # implicit no-action option. Dismissal is an ordinary consumer action.
        options = build_poll_options(actions, "Nessuna azione")
    except PollValidationError as err:
        raise CorrelatedSendError("invalid_request") from err
    runtime = entry.runtime_data
    question = f"{title}\n\n{message}" if title else message
    if len(question) > 4096 or "\x00" in question:
        raise CorrelatedSendError("invalid_request")
    request = {
        "kind": "poll",
        "message": message,
        "title": title,
        "actions": actions,
        "settle_seconds": settle_seconds,
        "content_redacted": content_redacted,
        "origin": "application",
    }

    async def transmit():
        if getattr(entry.state, "value", entry.state) != "loaded":
            raise CorrelatedSendError("channel_unavailable")
        contact = runtime.channel_registry.resolve_conversation(conversation_id)
        if contact is None:
            raise CorrelatedSendError("invalid_conversation")
        subentry = entry.subentries.get(contact.subentry_id)
        if subentry is None:
            raise CorrelatedSendError("invalid_conversation")
        redacted = content_redacted or bool(
            subentry.data.get("observation_content_redacted")
        )
        result = await async_send_observed(
            entry,
            conversation_id,
            "direct",
            {
                "kind": "poll",
                "text": question,
                "options": [item.title for item in options],
            },
            lambda: runtime.client.async_send_poll(
                contact.recipient, question, [item.title for item in options]
            ),
            origin="application",
            client_request_id=client_request_id,
            content_redacted=redacted,
        )
        if result.id is None or result.outcome != "accepted":
            raise CorrelatedSendError("outcome_unknown", status="unknown")
        try:
            # Both private mappings must be durable before reporting success.
            # A failure after sending is uncertain and cannot trigger a resend.
            token = await runtime.channel_registry.async_remember_message(
                result.id, conversation_id
            )
            await runtime.poll_manager.async_register_poll(
                result.id,
                result.chat_id,
                options,
                settle_seconds,
                contact.person_entity_id,
                origin="application",
                client_request_id=client_request_id,
                emit_legacy_action=False,
            )
        except Exception as err:
            raise CorrelatedSendError("outcome_unknown", status="unknown") from err
        return {"status": "accepted", "message_id": token}

    return await runtime.correlated_sends.async_execute(
        conversation_id, client_request_id, request, transmit
    )
