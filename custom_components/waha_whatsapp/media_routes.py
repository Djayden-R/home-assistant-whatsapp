"""Fresh, exact media route authorization shared by transfers and sends."""

from __future__ import annotations

import json
import re
import time
from collections.abc import Awaitable, Callable
from typing import Any

from .general_groups import GeneralGroupError, participant_alias
from .media import MediaError

MIN_MEDIA_GOWS_VERSION = (2026, 9, 1)


def media_observation_binding(
    entry: Any, conversation_id: str, *, raw_participant: str | None = None
) -> str | None:
    """Bind an already authorized callback locally without delaying publication."""
    try:
        _require_enabled(entry)
    except MediaError:
        return None
    runtime = entry.runtime_data
    version = re.fullmatch(
        r"(\d{4})\.(\d{1,2})\.(\d+)(?:\+[0-9A-Za-z.-]+)?", runtime.server.version
    )
    if (
        (runtime.server.engine or "").upper() != "GOWS"
        or version is None
        or tuple(map(int, version.groups())) < MIN_MEDIA_GOWS_VERSION
    ):
        return None
    account = participant_alias(runtime.session.account_id)
    if account is None:
        return None
    contact = runtime.channel_registry.resolve_conversation(conversation_id)
    if contact is not None:
        route = ("direct", contact.subentry_id, contact.recipient)
    elif general := runtime.general_groups.resolve_conversation(conversation_id):
        route = ("general", general.subentry_id, general.group_jid, general.bot_account)
    elif group := runtime.guest_registry.resolve_group_conversation(conversation_id):
        route = ("guest_group", group)
        if raw_participant is not None:
            member = runtime.guest_registry.resolve_member(
                raw_participant, occurred_at=time.time()
            )
            if member is None:
                return None
            route += (member["participant_id"], member["membership_id"])
    elif private := runtime.guest_registry.resolve_route(conversation_id):
        route = ("guest_direct", conversation_id, private)
    else:
        return None
    return json.dumps([entry.data["session"], account, *route], separators=(",", ":"))


async def async_media_backend_ready(entry: Any) -> bool:
    """Verify the supported engine and connected account without changing WAHA."""
    try:
        await _async_backend_account(entry)
    except MediaError:
        return False
    return True


async def _async_backend_account(entry: Any) -> str:
    _require_enabled(entry)
    runtime = entry.runtime_data
    try:
        server = await runtime.client.async_get_server()
        session = await runtime.client.async_get_session()
    except Exception as err:
        raise MediaError("channel_unavailable", retryable=True) from err
    version = re.fullmatch(
        r"(\d{4})\.(\d{1,2})\.(\d+)(?:\+[0-9A-Za-z.-]+)?", server.version
    )
    if (
        not isinstance(server.engine, str)
        or server.engine.upper() != "GOWS"
        or version is None
        or tuple(map(int, version.groups())) < MIN_MEDIA_GOWS_VERSION
    ):
        raise MediaError("unsupported")
    account = participant_alias(session.account_id)
    if (
        session.status != "WORKING"
        or session.name != entry.data["session"]
        or account is None
        or account != participant_alias(runtime.session.account_id)
    ):
        raise MediaError("channel_unavailable", retryable=True)
    _require_enabled(entry)
    return account


def _require_enabled(entry: Any) -> None:
    if entry.options.get("media_enabled") is not True:
        raise MediaError("media_disabled")
    if getattr(entry.state, "value", entry.state) != "loaded":
        raise MediaError("channel_unavailable", retryable=True)


async def async_authorize_media(
    entry: Any, conversation_id: str, *, raw_participant: str | None = None
) -> str:
    """Return a private immutable account/destination binding, never a fallback."""
    account = await _async_backend_account(entry)
    runtime = entry.runtime_data
    contact = runtime.channel_registry.resolve_conversation(conversation_id)
    route: tuple[str, ...]
    if contact is not None:
        route = ("direct", contact.subentry_id, contact.recipient)
    elif general := runtime.general_groups.resolve_conversation(conversation_id):
        try:
            proof = await runtime.general_groups.async_verify(general)
        except Exception as err:
            raise MediaError("invalid_conversation") from err
        if raw_participant is not None and proof.identity(raw_participant) is None:
            raise MediaError("invalid_conversation")
        route = ("general", general.subentry_id, general.group_jid, general.bot_account)
    else:
        try:
            state = await runtime.guest_manager.async_reconcile(force=True)
        except Exception as err:
            raise MediaError("invalid_conversation") from err
        if not state.get("ready"):
            raise MediaError("invalid_conversation")
        guests = runtime.guest_registry
        group = guests.resolve_group_conversation(conversation_id)
        private = guests.resolve_route(conversation_id)
        if group is not None:
            if not await runtime.guest_manager.async_verify_group_destination(group):
                raise MediaError("invalid_conversation")
            member = None
            if raw_participant is not None:
                member = guests.resolve_member(raw_participant, occurred_at=time.time())
                if member is None or (
                    not await runtime.guest_manager.async_verify_guest_destination(
                        raw_participant
                    )
                ):
                    raise MediaError("invalid_conversation")
                if (
                    guests.resolve_member(raw_participant, occurred_at=time.time())
                    != member
                ):
                    raise MediaError("invalid_conversation")
            if guests.resolve_group_conversation(conversation_id) != group:
                raise MediaError("invalid_conversation")
            route = ("guest_group", group)
            if member is not None:
                route += (member["participant_id"], member["membership_id"])
        elif private is not None:
            if not await runtime.guest_manager.async_verify_guest_destination(private):
                raise MediaError("invalid_conversation")
            if guests.resolve_route(conversation_id) != private:
                raise MediaError("invalid_conversation")
            route = ("guest_direct", conversation_id, private)
        else:
            raise MediaError("invalid_conversation")
    _require_enabled(entry)
    return json.dumps([entry.data["session"], account, *route], separators=(",", ":"))


async def async_media_send_route(
    entry: Any,
    conversation_id: str,
    transmit: Callable[[str, str, bool], Awaitable[Any]],
) -> Any:
    """Fence configuration changes around fresh reads and guest sends."""
    await async_authorize_media(entry, conversation_id)
    runtime = entry.runtime_data
    contact = runtime.channel_registry.resolve_conversation(conversation_id)
    if contact is not None:
        subentry = entry.subentries.get(contact.subentry_id)
        return await transmit(
            f"{contact.recipient}@c.us",
            "direct",
            bool(subentry and subentry.data.get("observation_content_redacted")),
        )
    general = runtime.general_groups.resolve_conversation(conversation_id)
    if general is not None:
        try:
            await runtime.general_groups.async_verify(general)
        except GeneralGroupError as err:
            raise MediaError("invalid_conversation") from err
        subentry = entry.subentries.get(general.subentry_id)
        return await transmit(
            general.group_jid,
            "group",
            bool(subentry and subentry.data.get("observation_content_redacted")),
        )
    guests = runtime.guest_registry
    redacted = bool(entry.options.get("guest_group_observation_content_redacted"))
    if (group := guests.resolve_group_conversation(conversation_id)) is not None:
        if not await runtime.guest_manager.async_verify_group_destination(group):
            raise MediaError("invalid_conversation")
        if guests.resolve_group_conversation(conversation_id) != group:
            raise MediaError("invalid_conversation")
        return await transmit(group, "group", redacted)

    async def _send(destination: str) -> Any:
        _require_enabled(entry)
        return await transmit(destination, "direct", redacted)

    try:
        return await guests.async_with_route(
            conversation_id,
            runtime.guest_manager.async_verify_guest_destination,
            _send,
        )
    except ValueError as err:
        raise MediaError("invalid_conversation") from err
