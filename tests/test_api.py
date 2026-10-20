"""Tests for the provider-independent WAHA API client."""

from typing import Any

import pytest

from ._loader import load_integration_module

api = load_integration_module("api")


class FakeResponse:
    """Minimal aiohttp response context manager."""

    def __init__(self, status: int, data: Any) -> None:
        self.status = status
        self._data = data

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, traceback) -> None:
        return None

    async def json(self, *, content_type=None):
        return self._data

    async def text(self) -> str:
        return str(self._data)


class FakeSession:
    """Capture requests and return queued responses."""

    def __init__(self, *responses: FakeResponse) -> None:
        self.responses = list(responses)
        self.requests: list[dict[str, Any]] = []

    def request(self, method: str, url: str, **kwargs):
        self.requests.append({"method": method, "url": url, **kwargs})
        return self.responses.pop(0)


def make_client(session: FakeSession):
    """Create a client with stable test connection values."""
    return api.WahaClient(
        session,
        "secret-api-key",
        "house",
        base_url="http://waha.internal:3000/",
    )


async def test_activity_uses_exact_read_and_documented_presence_cleanup_payloads():
    session = FakeSession(*(FakeResponse(200, {}) for _ in range(5)))
    client = make_client(session)
    await client.async_mark_seen("19999999999@c.us", "false_19999999999@c.us_EXAMPLE01")
    await client.async_mark_seen(
        "123456789@g.us",
        "false_123456789@g.us_EXAMPLE02_18888888888@c.us",
        participant="18888888888@c.us",
    )
    await client.async_set_presence("19999999999@c.us", "typing")
    await client.async_set_presence("19999999999@c.us", "paused")
    await client.async_set_presence(None, "offline")
    assert session.requests[0]["json"] == {
        "session": "house",
        "chatId": "19999999999@c.us",
        "messageIds": ["false_19999999999@c.us_EXAMPLE01"],
    }
    assert session.requests[1]["json"]["participant"] == "18888888888@c.us"
    assert len(session.requests[1]["json"]["messageIds"]) == 1
    assert session.requests[2]["json"] == {
        "presence": "typing",
        "chatId": "19999999999@c.us",
    }
    assert session.requests[3]["json"] == {
        "presence": "paused",
        "chatId": "19999999999@c.us",
    }
    assert session.requests[4]["json"] == {"presence": "offline"}
    assert session.requests[4]["url"].endswith("/api/house/presence")


@pytest.mark.parametrize("data", [[], "unexpected", 7])
async def test_activity_malformed_successful_response_retains_uncertainty(data):
    client = make_client(FakeSession(FakeResponse(200, data)))
    with pytest.raises(api.WahaResponseError):
        await client.async_set_presence("19999999999@c.us", "typing")


async def test_activity_api_refuses_broad_reads_and_chat_scoped_offline():
    session = FakeSession()
    client = make_client(session)
    for chat, message, participant in (
        ("19999999999@c.us", "", None),
        ("123456789@g.us", "example", None),
    ):
        with pytest.raises(ValueError, match="exact message|observed participant"):
            await client.async_mark_seen(chat, message, participant=participant)
    with pytest.raises(ValueError, match="Invalid activity presence"):
        await client.async_set_presence("19999999999@c.us", "offline")
    assert not session.requests


@pytest.mark.asyncio
async def test_get_server() -> None:
    """The account check reads WAHA version metadata with its API key."""
    session = FakeSession(
        FakeResponse(
            200,
            {"version": "2026.7.1", "engine": "GOWS", "tier": "PLUS"},
        )
    )
    client = make_client(session)

    server = await client.async_get_server()

    assert server.version == "2026.7.1"
    assert server.engine == "GOWS"
    assert session.requests[0]["url"] == "http://waha.internal:3000/api/version"
    assert session.requests[0]["headers"]["X-Api-Key"] == "secret-api-key"


@pytest.mark.asyncio
async def test_get_configured_session() -> None:
    """The configured session is resolved from the WAHA session list."""
    session = FakeSession(
        FakeResponse(
            200,
            [
                {"name": "other", "status": "STOPPED"},
                {
                    "name": "house",
                    "status": "WORKING",
                    "me": {"id": "393330000000@c.us", "pushName": "Home"},
                },
            ],
        )
    )
    client = make_client(session)

    result = await client.async_get_session()

    assert result.name == "house"
    assert result.status == "WORKING"
    assert result.push_name == "Home"
    assert session.requests[0]["params"] == {"all": "true"}


@pytest.mark.asyncio
async def test_missing_session_has_dedicated_error() -> None:
    """A typo in the session name produces a useful config-flow error."""
    client = make_client(FakeSession(FakeResponse(200, [])))

    with pytest.raises(api.WahaSessionNotFoundError, match="house"):
        await client.async_get_session()


@pytest.mark.asyncio
async def test_send_text_payload() -> None:
    """Free-form text uses WAHA's documented sendText payload."""
    session = FakeSession(FakeResponse(200, {"key": {"id": "ABCD1234"}}))
    client = make_client(session)

    result = await client.async_send_text(
        "+39 333 123 4567", "Door open", link_preview=False
    )

    assert result.id == "ABCD1234"
    assert result.chat_id == "393331234567@c.us"
    assert session.requests[0]["url"].endswith("/api/sendText")
    assert session.requests[0]["json"] == {
        "session": "house",
        "chatId": "393331234567@c.us",
        "text": "Door open",
        "linkPreview": False,
    }


@pytest.mark.asyncio
async def test_send_text_with_quoted_reply() -> None:
    """Quoted replies pass the private WAHA message ID to sendText."""
    session = FakeSession(FakeResponse(200, {"id": "REPLY-ID"}))

    await make_client(session).async_send_text(
        "393331234567", "Pong", reply_to_message_id="false_chat_OLD-ID"
    )

    assert session.requests[0]["json"]["reply_to"] == "false_chat_OLD-ID"


@pytest.mark.asyncio
async def test_gows_nested_message_id_is_supported() -> None:
    """GOWS engine variants may nest the WhatsApp message ID."""
    session = FakeSession(
        FakeResponse(200, {"_data": {"id": {"id": "GOWS-MESSAGE-ID"}}})
    )
    result = await make_client(session).async_send_text("393331234567", "Hello")

    assert result.id == "GOWS-MESSAGE-ID"


@pytest.mark.asyncio
async def test_send_single_selection_poll_payload() -> None:
    """Actionable notifications use WAHA's single-selection poll endpoint."""
    session = FakeSession(FakeResponse(200, {"id": "POLL-ID"}))
    client = make_client(session)

    result = await client.async_send_poll(
        "+39 333 123 4567",
        "Sleep mode\n\nCancel before it starts?",
        ["Cancel", "Keep scheduled"],
    )

    assert result.id == "POLL-ID"
    assert session.requests[0]["url"].endswith("/api/sendPoll")
    assert session.requests[0]["json"] == {
        "session": "house",
        "chatId": "393331234567@c.us",
        "poll": {
            "name": "Sleep mode\n\nCancel before it starts?",
            "options": ["Cancel", "Keep scheduled"],
            "multipleAnswers": False,
        },
    }


@pytest.mark.asyncio
async def test_resolve_lid_uses_session_mapping_api() -> None:
    """A GOWS LID is verified against WAHA before recipient correlation."""
    session = FakeSession(
        FakeResponse(
            200,
            {"lid": "178563278901234@lid", "pn": "393331234567@c.us"},
        )
    )

    result = await make_client(session).async_resolve_lid("178563278901234@lid")

    assert result == "393331234567@c.us"
    assert session.requests[0]["method"] == "GET"
    assert session.requests[0]["url"].endswith("/api/house/lids/178563278901234%40lid")


@pytest.mark.asyncio
async def test_unmapped_lid_is_not_treated_as_a_recipient() -> None:
    """WAHA must provide a phone-number mapping for an alternate identity."""
    session = FakeSession(FakeResponse(200, {"lid": "178563278901234@lid", "pn": None}))

    assert await make_client(session).async_resolve_lid("178563278901234@lid") is None


@pytest.mark.asyncio
async def test_ensure_webhook_preserves_unrelated_session_config() -> None:
    """Automatic poll setup merges rather than replacing user webhooks."""
    existing = {"url": "http://example.local/hook", "events": ["message"]}
    session = FakeSession(
        FakeResponse(
            200,
            {
                "name": "house",
                "config": {
                    "metadata": {"house": "test"},
                    "webhooks": [existing],
                },
            },
        ),
        FakeResponse(200, {"name": "house"}),
    )
    client = make_client(session)

    changed = await client.async_ensure_webhook(
        "http://homeassistant:8123/api/webhook/private", "hmac-secret"
    )

    assert changed is True
    assert session.requests[0]["method"] == "GET"
    assert session.requests[1]["method"] == "PUT"
    assert session.requests[1]["json"] == {
        "name": "house",
        "config": {
            "metadata": {"house": "test"},
            "webhooks": [
                existing,
                {
                    "url": "http://homeassistant:8123/api/webhook/private",
                    "events": [
                        "poll.vote",
                        "poll.vote.failed",
                        "message",
                        "message.reaction",
                        "session.status",
                    ],
                    "hmac": {"key": "hmac-secret"},
                    "retries": {
                        "policy": "constant",
                        "delaySeconds": 1,
                        "attempts": 3,
                    },
                },
            ],
        },
    }


@pytest.mark.asyncio
async def test_ensure_webhook_is_idempotent() -> None:
    """An already-correct private webhook does not update the WAHA session."""
    desired = {
        "url": "http://homeassistant:8123/api/webhook/private",
        "events": [
            "poll.vote",
            "poll.vote.failed",
            "message",
            "message.reaction",
            "session.status",
        ],
        "hmac": {"key": "hmac-secret"},
        "retries": {"policy": "constant", "delaySeconds": 1, "attempts": 3},
        "customHeaders": None,
    }
    session = FakeSession(
        FakeResponse(200, {"name": "house", "config": {"webhooks": [desired]}})
    )

    changed = await make_client(session).async_ensure_webhook(
        desired["url"], "hmac-secret"
    )

    assert changed is False
    assert len(session.requests) == 1


@pytest.mark.asyncio
async def test_group_webhook_subscription_is_explicitly_opt_in() -> None:
    session = FakeSession(
        FakeResponse(200, {"name": "house", "config": {"webhooks": []}}),
        FakeResponse(200, {"name": "house"}),
    )
    await make_client(session).async_ensure_webhook(
        "http://homeassistant:8123/api/webhook/private",
        "hmac-secret",
        group_events=True,
    )
    events = session.requests[1]["json"]["config"]["webhooks"][0]["events"]
    assert "poll.vote" in events
    assert "group.v2.participants" in events
    assert "group.v2.participants.join-request" in events


@pytest.mark.asyncio
async def test_authentication_error() -> None:
    """Authentication failures use a dedicated exception for HA reauth."""
    session = FakeSession(FakeResponse(401, {"message": "Bad API key"}))
    client = make_client(session)

    with pytest.raises(api.WahaAuthenticationError, match="Bad API key"):
        await client.async_get_server()


@pytest.mark.asyncio
async def test_create_group_requires_a_returned_group_id() -> None:
    """Creation returns only the exact identity to persist; no name-based lookup."""
    session = FakeSession(FakeResponse(201, {"JID": "123456@g.us"}))

    group_id = await make_client(session).async_create_group(
        "Casita — Guests", ["393331234567@c.us"]
    )

    assert group_id == "123456@g.us"
    assert session.requests[0]["method"] == "POST"
    assert session.requests[0]["url"].endswith("/api/house/groups")
    assert session.requests[0]["json"] == {
        "name": "Casita — Guests",
        "participants": [{"id": "393331234567@c.us"}],
    }


@pytest.mark.asyncio
async def test_create_group_rejects_invalid_raw_gows_jid_without_retry() -> None:
    """A malformed GOWS create response must not trigger a second write."""
    session = FakeSession(FakeResponse(201, {"JID": "wrong@c.us"}))

    with pytest.raises(api.WahaResponseError, match="valid group ID"):
        await make_client(session).async_create_group("Guests", ["1@c.us"])

    assert len(session.requests) == 1


@pytest.mark.asyncio
async def test_send_group_text_restricts_group_jid_without_changing_direct_send() -> (
    None
):
    session = FakeSession(FakeResponse(200, {"id": "sent-1"}))
    client = make_client(session)
    result = await client.async_send_group_text(
        "120363123456789-1234567890@g.us", "Welcome"
    )
    assert result.chat_id == "120363123456789-1234567890@g.us"
    assert session.requests[0]["json"]["chatId"] == result.chat_id
    with pytest.raises(ValueError, match="group ID"):
        await client.async_send_group_text("../../wrong@g.us", "No")
    with pytest.raises(ValueError, match="Phone recipients"):
        await client.async_send_text("120363123456789@g.us", "Not a direct recipient")
    assert len(session.requests) == 1


@pytest.mark.asyncio
async def test_create_and_read_legacy_hyphenated_group_id() -> None:
    """Older WhatsApp groups use a numeric-numeric JID local part."""
    group_id = "39333-1234567890@g.us"
    session = FakeSession(
        FakeResponse(201, {"id": group_id}),
        FakeResponse(200, {"id": group_id, "subject": "Guests"}),
    )
    client = make_client(session)

    assert await client.async_create_group("Guests", ["111@c.us"]) == group_id
    assert (await client.async_get_group(group_id))["id"] == group_id
    assert session.requests[1]["url"].endswith("/groups/39333-1234567890%40g.us")


@pytest.mark.asyncio
async def test_read_raw_gows_group_info_requires_exact_saved_jid() -> None:
    """GOWS returns an unwrapped Go GroupInfo rather than a generic id field."""
    session = FakeSession(FakeResponse(200, {"JID": "123456@g.us"}))
    assert (await make_client(session).async_get_group("123456@g.us"))["JID"] == (
        "123456@g.us"
    )

    mismatch = make_client(FakeSession(FakeResponse(200, {"JID": "other@g.us"})))
    with pytest.raises(api.WahaResponseError, match="invalid group information"):
        await mismatch.async_get_group("123456@g.us")


@pytest.mark.asyncio
async def test_group_id_path_injection_is_rejected_before_request() -> None:
    session = FakeSession()
    client = make_client(session)

    with pytest.raises(ValueError, match="Invalid WAHA group ID"):
        await client.async_get_group("../39333-1234567890@g.us")
    assert session.requests == []


@pytest.mark.asyncio
async def test_create_group_rejects_missing_id_without_retry() -> None:
    session = FakeSession(FakeResponse(200, {"subject": "Casita — Guests"}))

    with pytest.raises(api.WahaResponseError, match="valid group ID"):
        await make_client(session).async_create_group("Casita — Guests", ["1@c.us"])

    assert len(session.requests) == 1


@pytest.mark.asyncio
async def test_get_exact_group_and_roster() -> None:
    session = FakeSession(
        FakeResponse(200, {"id": "123456@g.us", "subject": "Renamed"}),
        FakeResponse(
            200,
            [
                {"id": "111@lid", "pn": "393331234567@c.us", "role": "admin"},
                {"id": "222@c.us", "role": "participant"},
            ],
        ),
    )
    client = make_client(session)

    assert (await client.async_get_group("123456@g.us"))["subject"] == "Renamed"
    assert await client.async_get_group_participants("123456@g.us") == [
        api.WahaGroupParticipant("111@lid", "admin", "393331234567@c.us"),
        api.WahaGroupParticipant("222@c.us", "participant", None),
    ]
    assert session.requests[0]["url"].endswith("/groups/123456%40g.us")
    assert session.requests[1]["url"].endswith("/groups/123456%40g.us/participants/v2")


@pytest.mark.asyncio
async def test_roster_rejects_unexpected_role_and_alias() -> None:
    session = FakeSession(
        FakeResponse(200, [{"id": "111@lid", "role": "owner"}]),
        FakeResponse(200, [{"id": "111@lid", "role": "admin", "pn": "other@lid"}]),
    )
    client = make_client(session)

    with pytest.raises(api.WahaResponseError, match="participant"):
        await client.async_get_group_participants("123456@g.us")
    with pytest.raises(api.WahaResponseError, match="participant"):
        await client.async_get_group_participants("123456@g.us")


@pytest.mark.asyncio
async def test_add_and_promote_use_participant_dto() -> None:
    session = FakeSession(FakeResponse(200, True), FakeResponse(200, True))
    client = make_client(session)

    await client.async_add_group_participants("123456@g.us", ["111@c.us"])
    await client.async_promote_group_admins("123456@g.us", ["111@c.us"])

    assert session.requests[0]["url"].endswith("/participants/add")
    assert session.requests[1]["url"].endswith("/admin/promote")
    assert all(
        request["json"] == {"participants": [{"id": "111@c.us"}]}
        for request in session.requests
    )


@pytest.mark.asyncio
async def test_add_and_promote_reject_explicit_failure() -> None:
    session = FakeSession(
        FakeResponse(200, False), FakeResponse(200, {"success": False})
    )
    client = make_client(session)

    with pytest.raises(api.WahaResponseError, match="did not add"):
        await client.async_add_group_participants("123456@g.us", ["111@c.us"])
    with pytest.raises(api.WahaResponseError, match="did not promote"):
        await client.async_promote_group_admins("123456@g.us", ["111@c.us"])


@pytest.mark.asyncio
async def test_bad_participant_batch_fails_before_write() -> None:
    session = FakeSession()
    client = make_client(session)

    with pytest.raises(ValueError, match="unique"):
        await client.async_add_group_participants(
            "123456@g.us", ["111@c.us", "111@c.us"]
        )
    with pytest.raises(ValueError, match="Invalid"):
        await client.async_promote_group_admins("123456@g.us", ["../bad"])
    assert session.requests == []


@pytest.mark.asyncio
async def test_group_security_set_and_read_back() -> None:
    session = FakeSession(
        *(FakeResponse(200, True) for _ in range(4)),
        FakeResponse(200, {"adminsOnly": True}),
        FakeResponse(200, {"adminsOnly": False}),
        FakeResponse(200, {"membersCanAddNewMember": False}),
        FakeResponse(200, {"newMembersApprovalRequired": True}),
    )
    client = make_client(session)

    await client.async_set_group_security(
        "123456@g.us",
        info_admin_only=True,
        messages_admin_only=False,
        members_can_add=False,
        membership_approval_required=True,
    )
    assert await client.async_get_group_security("123456@g.us") == {
        "info_admin_only": True,
        "messages_admin_only": False,
        "members_can_add": False,
        "membership_approval_required": True,
    }
    assert [request["json"] for request in session.requests[:4]] == [
        {"adminsOnly": True},
        {"adminsOnly": False},
        {"membersCanAddNewMember": False},
        {"newMembersApprovalRequired": True},
    ]
    assert [request["url"].rsplit("/", 1)[-1] for request in session.requests] == [
        "info-admin-only",
        "messages-admin-only",
        "member-add-mode",
        "membership-approval",
    ] * 2


@pytest.mark.asyncio
async def test_group_security_refuses_failed_or_ambiguous_response() -> None:
    session = FakeSession(FakeResponse(200, False))
    with pytest.raises(api.WahaResponseError, match="did not update"):
        await make_client(session).async_set_group_security(
            "123456@g.us",
            info_admin_only=True,
            messages_admin_only=False,
            members_can_add=False,
            membership_approval_required=True,
        )
    assert len(session.requests) == 1

    session = FakeSession(FakeResponse(200, {"adminsOnly": "false"}))
    with pytest.raises(api.WahaResponseError, match="invalid"):
        await make_client(session).async_get_group_security("123456@g.us")


@pytest.mark.asyncio
@pytest.mark.parametrize("body", [None, [], "upstream unavailable"])
async def test_non_object_send_response_preserves_uncertain_outcome(body) -> None:
    """An ambiguous 2xx body must not become confirmed acceptance or trigger retry."""
    session = FakeSession(FakeResponse(200, body))
    result = await make_client(session).async_send_text("393331234567", "Hello")
    assert result.outcome == "unknown"
    assert result.id is None
    assert len(session.requests) == 1


@pytest.mark.asyncio
async def test_successful_object_without_message_id_is_still_accepted() -> None:
    """A missing optional tracking ID is not evidence of a failed submission."""
    session = FakeSession(FakeResponse(200, {}))
    result = await make_client(session).async_send_text("393331234567", "Hello")
    assert result.outcome == "accepted"
    assert result.id is None
    assert len(session.requests) == 1


@pytest.mark.asyncio
async def test_non_json_success_is_unknown_without_retry() -> None:
    """A proxy error page with HTTP 200 must retain uncertainty for keyed sends."""

    class NonJsonResponse(FakeResponse):
        async def json(self, *, content_type=None):
            raise ValueError("not JSON")

    session = FakeSession(NonJsonResponse(200, "upstream unavailable"))
    result = await make_client(session).async_send_text("393331234567", "Hello")
    assert result.outcome == "unknown"
    assert len(session.requests) == 1
