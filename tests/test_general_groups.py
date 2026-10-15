"""General destinations stay explicit, account-bound and independent of guests."""

import sys
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from ._loader import PACKAGE_NAME, load_integration_module

groups = load_integration_module("general_groups")
api = load_integration_module("api")
channel = load_integration_module("channel_registry")

GROUP = "120363123456789-1234567890@g.us"
OTHER_GROUP = "120363111111111@g.us"
BOT = "393331111111@c.us"
MEMBER = "441234567890@c.us"
LID = "123456789@lid"


class FakeStore:
    def __init__(self):
        self.data = None

    async def async_load(self):
        return deepcopy(self.data)

    async def async_save(self, data):
        self.data = deepcopy(data)


class Client(api.WahaClient):
    """Exercise real API parsing and group sends while replacing HTTP only."""

    def __init__(self):
        super().__init__(None, "secret-key", "house", base_url="http://waha.invalid")
        self.account = BOT
        self.status = "WORKING"
        self.engine = "GOWS"
        self.version = "2026.9.1"
        self.group_response = GROUP
        self.roster = [
            {"id": BOT, "role": "participant", "pn": BOT},
            {"id": LID, "role": "participant", "pn": MEMBER},
        ]
        self.requests = []

    async def _request(self, method, path, **kwargs):
        self.requests.append((method, path, kwargs))
        if path == "/api/version":
            return {"version": self.version, "engine": self.engine}
        if path == "/api/sessions":
            return [
                {"name": "house", "status": self.status, "me": {"id": self.account}}
            ]
        if path.endswith("/participants/v2"):
            return deepcopy(self.roster)
        if method == "GET" and "/groups/" in path:
            return {"JID": self.group_response}
        if method == "POST" and path == "/api/sendText":
            return {"id": "false_group_accepted"}
        raise AssertionError((method, path, kwargs))


def make_manager(*, enabled=True, guest_group=None):
    subentry = SimpleNamespace(
        subentry_type="group",
        subentry_id="group-contact",
        title="General group",
        data={"group_id": GROUP, "enabled": enabled, "bot_account": BOT},
    )
    entry = SimpleNamespace(
        entry_id="entry-a",
        data={"session": "house", "webhook_secret": "private-channel-secret"},
        subentries={subentry.subentry_id: subentry},
    )
    client = Client()
    registry = channel.ChannelRegistry(
        SimpleNamespace(data={}), entry, client, store=FakeStore()
    )
    manager = groups.GeneralGroupManager(
        None,
        entry,
        client,
        registry,
        SimpleNamespace(saved_group_jid=guest_group) if guest_group else None,
    )
    registry.set_extra_route_resolver(manager.conversation_destination)
    return manager, entry, client, registry


@pytest.mark.parametrize("value", [GROUP, GROUP.upper(), GROUP.removesuffix("@g.us")])
def test_explicit_group_normalization(value):
    assert groups.normalize_group_id(value) == GROUP


@pytest.mark.parametrize(
    "value", [None, 123, "", "@g.us", "name@g.us", "../1@g.us", MEMBER, "１２３@g.us"]
)
def test_group_input_never_accepts_direct_or_path_destinations(value):
    with pytest.raises(groups.GeneralGroupError, match="invalid_group_id"):
        groups.normalize_group_id(value)


@pytest.mark.asyncio
async def test_review_and_send_use_exact_group_and_real_group_api():
    manager, entry, client, _registry = make_manager()
    proof = await manager.async_review(GROUP)
    assert proof.bot_account == BOT
    assert proof.identity(LID) == proof.identity(MEMBER) == MEMBER
    destination = manager.resolve_subentry("group-contact")
    result = await manager.async_send_text(
        destination.conversation_id, "Group message", reply_to="raw-quote"
    )
    assert result.chat_id == GROUP
    assert client.requests[-1] == (
        "POST",
        "/api/sendText",
        {
            "json": {
                "session": "house",
                "chatId": GROUP,
                "text": "Group message",
                "linkPreview": True,
                "reply_to": "raw-quote",
            }
        },
    )
    assert not any(
        "create" in path or "security" in path for _, path, _ in client.requests
    )
    assert list(entry.subentries) == ["group-contact"]


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["disable", "remove", "destination", "account"])
async def test_stale_routes_fail_before_network(change):
    manager, entry, client, registry = make_manager()
    destination = manager.resolve_subentry("group-contact")
    token = await registry.async_remember_message(
        "message-a", destination.conversation_id
    )
    if change == "remove":
        entry.subentries.clear()
    elif change == "disable":
        entry.subentries["group-contact"].data["enabled"] = False
    elif change == "destination":
        entry.subentries["group-contact"].data["group_id"] = OTHER_GROUP
    else:
        entry.subentries["group-contact"].data["bot_account"] = MEMBER
    assert manager.resolve_conversation(destination.conversation_id) is None
    assert registry.resolve_message(token, destination.conversation_id) is None
    with pytest.raises(groups.GeneralGroupError, match="invalid_conversation"):
        await manager.async_send_text(destination.conversation_id, "No send")
    assert client.requests == []


@pytest.mark.asyncio
async def test_routes_and_quote_tokens_survive_reload_with_intact_secret():
    manager, entry, client, registry = make_manager()
    destination = manager.resolve_subentry("group-contact")
    token = await registry.async_remember_message(
        "message-a", destination.conversation_id
    )
    restored = channel.ChannelRegistry(
        registry._hass, entry, client, store=registry._store
    )
    manager2 = groups.GeneralGroupManager(None, entry, client, restored)
    await restored.async_start()
    assert restored.resolve_message(token, destination.conversation_id) is None
    restored.set_extra_route_resolver(manager2.conversation_destination)
    assert manager2.resolve_subentry("group-contact") == destination
    assert restored.resolve_message(token, destination.conversation_id) == "message-a"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "change", ["account", "membership", "unsupported", "wrong_group", "ambiguous"]
)
async def test_fresh_eligibility_failure_never_sends(change):
    manager, _entry, client, _registry = make_manager()
    destination = manager.resolve_subentry("group-contact")
    if change == "account":
        client.account = MEMBER
    elif change == "membership":
        client.roster[0]["role"] = "left"
    elif change == "unsupported":
        client.version = "2026.8.1"
    elif change == "wrong_group":
        client.group_response = OTHER_GROUP
    else:
        client.roster.append(
            {"id": "987654321@lid", "pn": MEMBER, "role": "participant"}
        )
    with pytest.raises((groups.GeneralGroupError, api.WahaError)):
        await manager.async_send_text(destination.conversation_id, "No send")
    assert all(method == "GET" for method, _, _ in client.requests)


@pytest.mark.asyncio
async def test_guest_group_cannot_be_adopted_or_reused():
    manager, _entry, client, _registry = make_manager(guest_group=GROUP)
    assert manager.resolve_subentry("group-contact") is None
    with pytest.raises(groups.GeneralGroupError, match="managed_group_reserved"):
        await manager.async_review(GROUP)
    assert client.requests == []


@pytest.mark.parametrize(
    "duplicate_id", [GROUP, GROUP.upper(), GROUP.removesuffix("@g.us")]
)
def test_duplicate_configured_groups_fail_closed(duplicate_id):
    manager, entry, _client, _registry = make_manager()
    duplicate = deepcopy(entry.subentries["group-contact"])
    duplicate.subentry_id = "duplicate"
    duplicate.data["group_id"] = duplicate_id
    entry.subentries["duplicate"] = duplicate
    assert manager.resolve_subentry("group-contact") is None
    assert manager.resolve_subentry("duplicate") is None
    assert manager.resolve_group(GROUP) is None


def make_group_flow(monkeypatch, *, enabled=True):
    """Use HA's real flow contracts with only entry/transport state replaced."""
    pytest.importorskip("homeassistant")
    sys.modules[PACKAGE_NAME].WahaConfigEntry = object
    flows = load_integration_module("config_flow")
    manager, entry, client, registry = make_manager(enabled=enabled)
    entry.state = flows.ConfigEntryState.LOADED
    entry.title = "Connection"
    entry.runtime_data = SimpleNamespace(general_groups=manager)
    flow = flows.GeneralGroupSubentryFlow()
    flow.hass = SimpleNamespace()
    flow.context = {"source": "user"}
    monkeypatch.setattr(type(flow), "_get_entry", lambda self: entry)
    return flow, manager, entry, client, registry


@pytest.mark.asyncio
async def test_group_flow_verifies_account_and_saves_explicit_binding(monkeypatch):
    flow, _manager, entry, client, _registry = make_group_flow(monkeypatch)
    entry.subentries.clear()
    result = await flow.async_step_user(
        {"name": "Group", "group_id": GROUP.removesuffix("@g.us"), "enabled": True}
    )
    assert result["type"] == "create_entry"
    assert result["data"] == {
        "group_id": GROUP,
        "enabled": True,
        "bot_account": BOT,
        "observation_content_redacted": False,
    }
    assert all(method == "GET" for method, _, _ in client.requests)


@pytest.mark.asyncio
async def test_group_flow_cannot_enable_a_group_without_membership(monkeypatch):
    flow, _manager, entry, client, _registry = make_group_flow(monkeypatch)
    entry.subentries.clear()
    client.roster[0]["role"] = "left"
    result = await flow.async_step_user(
        {"name": "Group", "group_id": GROUP, "enabled": True}
    )
    assert result["type"] == "form"
    assert result["errors"] == {"base": "unverified_group_membership"}


@pytest.mark.asyncio
@pytest.mark.parametrize("saved_id", [GROUP.upper(), GROUP.removesuffix("@g.us")])
async def test_group_flow_rejects_equivalent_saved_group_ids(monkeypatch, saved_id):
    flow, _manager, entry, client, _registry = make_group_flow(monkeypatch)
    entry.subentries["group-contact"].data["group_id"] = saved_id
    result = await flow.async_step_user(
        {"name": "Duplicate", "group_id": GROUP, "enabled": True}
    )
    assert result["type"] == "abort"
    assert result["reason"] == "group_already_configured"
    assert client.requests == []


@pytest.mark.asyncio
async def test_group_flow_disable_works_offline_and_preserves_account_binding(
    monkeypatch,
):
    flow, _manager, entry, client, _registry = make_group_flow(monkeypatch)
    existing = entry.subentries["group-contact"]
    monkeypatch.setattr(type(flow), "_get_reconfigure_subentry", lambda self: existing)
    flow.context = {"source": "reconfigure"}
    update_subentry = Mock(return_value=True)
    reload_entry = AsyncMock()
    flow.hass.config_entries = SimpleNamespace(
        async_update_subentry=update_subentry,
        async_reload=reload_entry,
    )
    client.account = MEMBER
    result = await flow.async_step_reconfigure(
        {
            "name": "Group",
            "group_id": GROUP,
            "enabled": False,
            "observation_content_redacted": True,
        }
    )
    assert result["type"] == "abort"
    assert result["reason"] == "reconfigure_successful"
    update_subentry.assert_called_once()
    data = update_subentry.call_args.kwargs["data"]
    assert data["enabled"] is False
    assert data["bot_account"] == BOT
    assert data["observation_content_redacted"] is True
    reload_entry.assert_not_called()
    assert client.requests == []
