"""Media permission checks use fresh routes, accounts and principal state."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from ._loader import load_integration_module

routes = load_integration_module("media_routes")
media = load_integration_module("media")


def _entry():
    contact = SimpleNamespace(subentry_id="contact", recipient="390000000001")
    client = SimpleNamespace(
        async_get_server=AsyncMock(
            return_value=SimpleNamespace(version="2026.9.1", engine="GOWS")
        ),
        async_get_session=AsyncMock(
            return_value=SimpleNamespace(
                name="default", status="WORKING", account_id="390000000099@c.us"
            )
        ),
    )
    runtime = SimpleNamespace(
        client=client,
        server=SimpleNamespace(version="2026.9.1", engine="GOWS"),
        session=SimpleNamespace(account_id="390000000099@c.us"),
        channel_registry=SimpleNamespace(resolve_conversation=lambda _id: contact),
        general_groups=SimpleNamespace(resolve_conversation=lambda _id: None),
        guest_manager=SimpleNamespace(async_reconcile=AsyncMock(return_value={})),
        guest_registry=SimpleNamespace(
            resolve_group_conversation=lambda _id: None,
            resolve_route=lambda _id: None,
        ),
    )
    return SimpleNamespace(
        options={"media_enabled": True},
        state="loaded",
        data={"session": "default"},
        runtime_data=runtime,
        subentries={
            "contact": SimpleNamespace(data={"observation_content_redacted": True})
        },
    )


@pytest.mark.asyncio
async def test_observation_binding_matches_fresh_direct_binding():
    entry = _entry()
    assert routes.media_observation_binding(entry, "conversation") == (
        await routes.async_authorize_media(entry, "conversation")
    )
    entry.runtime_data.client.async_get_server.assert_awaited_once()


@pytest.mark.asyncio
async def test_disabled_media_does_no_backend_io():
    entry = _entry()
    entry.options["media_enabled"] = False
    assert routes.media_observation_binding(entry, "conversation") is None
    assert not await routes.async_media_backend_ready(entry)
    entry.runtime_data.client.async_get_server.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "change", ["account", "session", "engine", "version", "unload"]
)
async def test_backend_and_loaded_entry_are_freshly_verified(change):
    entry = _entry()
    client = entry.runtime_data.client
    if change == "account":
        client.async_get_session.return_value.account_id = "390000000002@c.us"
    elif change == "session":
        client.async_get_session.return_value.name = "other"
    elif change == "engine":
        client.async_get_server.return_value.engine = "WEBJS"
    elif change == "version":
        client.async_get_server.return_value.version = "2026.8.1"
    else:
        entry.state = "not_loaded"
    assert not await routes.async_media_backend_ready(entry)
    with pytest.raises(media.MediaError):
        await routes.async_authorize_media(entry, "conversation")


@pytest.mark.asyncio
async def test_unknown_route_and_expired_guest_fail_closed():
    entry = _entry()
    entry.runtime_data.channel_registry.resolve_conversation = lambda _id: None
    with pytest.raises(media.MediaError, match="invalid_conversation"):
        await routes.async_authorize_media(entry, "old_guest")
    entry.runtime_data.guest_manager.async_reconcile.assert_awaited_once_with(
        force=True
    )


@pytest.mark.asyncio
async def test_group_original_sender_requires_current_verified_roster():
    entry = _entry()
    entry.runtime_data.channel_registry.resolve_conversation = lambda _id: None
    group = SimpleNamespace(
        subentry_id="group",
        group_jid="120000000000@g.us",
        bot_account="390000000099@c.us",
    )
    entry.runtime_data.general_groups = SimpleNamespace(
        resolve_conversation=lambda _id: group,
        async_verify=AsyncMock(return_value=SimpleNamespace(identity=lambda _id: None)),
    )
    with pytest.raises(media.MediaError, match="invalid_conversation"):
        await routes.async_authorize_media(
            entry, "group_conversation", raw_participant="390000000001@c.us"
        )


@pytest.mark.asyncio
async def test_direct_send_uses_only_resolved_destination_and_redaction():
    entry = _entry()
    transmit = AsyncMock(return_value="accepted")
    assert (
        await routes.async_media_send_route(entry, "conversation", transmit)
        == "accepted"
    )
    transmit.assert_awaited_once_with("390000000001@c.us", "direct", True)


@pytest.mark.asyncio
async def test_disable_during_fresh_backend_read_fences_send():
    entry = _entry()

    async def disable():
        entry.options["media_enabled"] = False
        return SimpleNamespace(
            name="default", status="WORKING", account_id="390000000099@c.us"
        )

    entry.runtime_data.client.async_get_session.side_effect = disable
    transmit = AsyncMock()
    with pytest.raises(media.MediaError, match="media_disabled"):
        await routes.async_media_send_route(entry, "conversation", transmit)
    transmit.assert_not_called()
