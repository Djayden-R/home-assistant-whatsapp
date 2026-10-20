"""Actual HA activity schemas, public registration and administrator policy."""

import importlib
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from .test_activity import environment


def integration():
    pytest.importorskip("homeassistant")
    return importlib.import_module("custom_components.waha_whatsapp")


def test_public_schemas_enforce_strict_activity_contract():
    module = integration()
    import voluptuous as vol

    read = {
        "config_entry_id": "entry_example",
        "conversation_id": "direct_example",
        "message_id": "msg_example",
    }
    typing = {
        "config_entry_id": "entry_example",
        "conversation_id": "direct_example",
        "activity_id": "Activity._:01-",
        "state": "typing",
    }
    assert module.MARK_CONVERSATION_READ_SCHEMA(read) == read
    assert module.SET_CONVERSATION_TYPING_SCHEMA(typing) == typing
    for value in (3, 10, 15):
        assert (
            module.SET_CONVERSATION_TYPING_SCHEMA({**typing, "ttl_seconds": value})[
                "ttl_seconds"
            ]
            == value
        )
    for value in (True, False, "10", 10.0, 2, 16):
        with pytest.raises(vol.Invalid):
            module.SET_CONVERSATION_TYPING_SCHEMA({**typing, "ttl_seconds": value})
    with pytest.raises(vol.Invalid):
        module.SET_CONVERSATION_TYPING_SCHEMA(
            {**typing, "state": "paused", "ttl_seconds": 10}
        )
    assert (
        module.SET_CONVERSATION_TYPING_SCHEMA({**typing, "state": "paused"})["state"]
        == "paused"
    )
    for field in read:
        for value in ("", "a" * 257, 123, None):
            with pytest.raises(vol.Invalid):
                module.MARK_CONVERSATION_READ_SCHEMA({**read, field: value})


async def test_services_register_with_response_only_support(monkeypatch):
    module = integration()
    registrations = {}
    hass = SimpleNamespace(
        services=SimpleNamespace(
            async_register=lambda domain, name, handler, **options: (
                registrations.update({name: (domain, handler, options)})
            )
        )
    )
    monkeypatch.setattr(module, "register_media_views", lambda _hass: None)
    await module.async_setup(hass, {})
    for name, schema in (
        ("mark_conversation_read", module.MARK_CONVERSATION_READ_SCHEMA),
        ("set_conversation_typing", module.SET_CONVERSATION_TYPING_SCHEMA),
    ):
        domain, _handler, options = registrations[name]
        assert domain == "waha_whatsapp"
        assert options["supports_response"] is module.SupportsResponse.ONLY
        assert options["schema"] is schema


@pytest.mark.parametrize("user", [None, "inactive", "nonadmin", "no_context"])
@pytest.mark.parametrize("operation", ["read", "typing"])
async def test_handlers_refuse_unauthorized_before_manager_or_backend(
    monkeypatch, user, operation
):
    module = integration()
    env = await environment(monkeypatch)
    person = (
        None
        if user is None
        else SimpleNamespace(
            id="admin", is_active=user != "inactive", is_admin=user != "nonadmin"
        )
    )
    env.hass.auth = SimpleNamespace(async_get_user=AsyncMock(return_value=person))
    request = env.typing() if operation == "typing" else await env.remember()
    call = SimpleNamespace(
        hass=env.hass,
        data=request,
        context=SimpleNamespace(user_id=None if user == "no_context" else "admin"),
    )
    handler = (
        module._async_handle_set_typing
        if operation == "typing"
        else module._async_handle_mark_read
    )
    response = await handler(call)
    assert response["status"] == "failed"
    assert response["error_code"] == "not_authorized"
    assert response["schema_version"] == 1
    env.client.async_get_server.assert_not_awaited()
    env.client.async_mark_seen.assert_not_awaited()
    env.client.async_set_presence.assert_not_awaited()


async def test_handler_rechecks_revoked_admin_after_backend_await(monkeypatch):
    module = integration()
    env = await environment(monkeypatch)
    person = SimpleNamespace(id="admin", is_active=True, is_admin=True)
    env.hass.auth = SimpleNamespace(async_get_user=AsyncMock(return_value=person))
    monkeypatch.setattr(module, "_loaded_entry", lambda *_args: env.entry)

    async def get_session():
        person.is_active = False
        return env.client.async_get_session.return_value

    env.client.async_get_session.side_effect = get_session
    request = env.typing()
    call = SimpleNamespace(
        hass=env.hass, data=request, context=SimpleNamespace(user_id="admin")
    )
    result = await module._async_handle_set_typing(call)
    assert result["error_code"] == "not_authorized"
    env.client.async_set_presence.assert_not_awaited()


async def test_native_ha_timer_is_used(monkeypatch):
    module = integration()
    env = await environment(monkeypatch)
    schedules = []
    monkeypatch.setattr(
        "homeassistant.helpers.event.async_call_later",
        lambda hass, delay, callback: (
            schedules.append((hass, delay, callback)) or (lambda: None)
        ),
    )
    manager = module.ActivityManager(env.hass, env.entry)
    manager._ha_schedule(10, "callback")
    assert schedules == [(env.hass, 10, "callback")]
