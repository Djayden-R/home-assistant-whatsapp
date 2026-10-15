"""Async client for the WAHA HTTP API."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote

from aiohttp import ClientError, ClientSession

from .helpers import normalize_chat_id

REQUEST_TIMEOUT_SECONDS = 15


class WahaError(Exception):
    """Base exception for WAHA API failures."""


class WahaConnectionError(WahaError):
    """Raised when WAHA cannot be reached."""


class WahaAuthenticationError(WahaError):
    """Raised when WAHA rejects the API key."""


class WahaRequestError(WahaError):
    """Raised when WAHA rejects a request."""

    def __init__(self, message: str, status: int) -> None:
        """Initialize the request error."""
        super().__init__(message)
        self.status = status


class WahaResponseError(WahaError):
    """Raised when WAHA returns an unexpected response."""


class WahaSessionNotFoundError(WahaError):
    """Raised when the configured WAHA session does not exist."""


@dataclass(frozen=True, slots=True)
class WahaServer:
    """Details reported by a WAHA server."""

    version: str
    engine: str | None
    tier: str | None


@dataclass(frozen=True, slots=True)
class WahaSession:
    """Current state of a configured WAHA session."""

    name: str
    status: str
    account_id: str | None
    push_name: str | None


@dataclass(frozen=True, slots=True)
class WahaMessage:
    """Send result; malformed successful HTTP responses retain uncertainty."""

    id: str | None
    chat_id: str
    outcome: str = "accepted"


@dataclass(frozen=True, slots=True)
class WahaGroupParticipant:
    """One member in WAHA's cross-engine v2 group roster."""

    id: str
    role: str
    pn: str | None


class WahaClient:
    """Small async client for a local WAHA server."""

    def __init__(
        self,
        session: ClientSession,
        api_key: str,
        session_name: str,
        *,
        base_url: str,
    ) -> None:
        """Initialize the client."""
        self._session = session
        self._api_key = api_key
        self.session_name = session_name
        self.base_url = base_url.rstrip("/")

    async def async_get_server(self) -> WahaServer:
        """Validate credentials and fetch WAHA version information."""
        data = await self._request("GET", "/api/version")
        if not isinstance(data, dict):
            raise WahaResponseError("WAHA returned invalid version information")

        version = data.get("version")
        if not isinstance(version, str) or not version:
            raise WahaResponseError("WAHA did not return its version")
        return WahaServer(
            version=version,
            engine=_optional_string(data.get("engine")),
            tier=_optional_string(data.get("tier")),
        )

    async def async_get_session(self) -> WahaSession:
        """Fetch the configured WAHA session from the server."""
        data = await self._request("GET", "/api/sessions", params={"all": "true"})
        if not isinstance(data, list):
            raise WahaResponseError("WAHA returned an invalid session list")

        for item in data:
            if isinstance(item, dict) and item.get("name") == self.session_name:
                status = item.get("status")
                if not isinstance(status, str):
                    raise WahaResponseError("WAHA returned an invalid session status")
                me = item.get("me") if isinstance(item.get("me"), dict) else {}
                return WahaSession(
                    name=self.session_name,
                    status=status,
                    account_id=_optional_string(me.get("id")),
                    push_name=_optional_string(me.get("pushName")),
                )

        raise WahaSessionNotFoundError(
            f"WAHA session '{self.session_name}' does not exist"
        )

    async def async_send_text(
        self,
        recipient: str,
        text: str,
        *,
        link_preview: bool = True,
        reply_to_message_id: str | None = None,
    ) -> WahaMessage:
        """Send a free-form WhatsApp text message."""
        chat_id = normalize_chat_id(recipient)
        payload = {
            "session": self.session_name,
            "chatId": chat_id,
            "text": text,
            "linkPreview": link_preview,
        }
        if reply_to_message_id is not None:
            payload["reply_to"] = reply_to_message_id
        data = await self._request("POST", "/api/sendText", json=payload)
        return WahaMessage(
            id=_message_id(data),
            chat_id=chat_id,
            outcome="accepted" if isinstance(data, dict) else "unknown",
        )

    async def async_send_group_text(
        self,
        group_id: str,
        text: str,
        *,
        reply_to_message_id: str | None = None,
    ) -> WahaMessage:
        """Send to a validated group JID; caller verifies current eligibility."""
        if not _valid_group_id(group_id):
            raise ValueError("Invalid WAHA group ID")
        payload = {
            "session": self.session_name,
            "chatId": group_id,
            "text": text,
            "linkPreview": True,
        }
        if reply_to_message_id is not None:
            payload["reply_to"] = reply_to_message_id
        data = await self._request("POST", "/api/sendText", json=payload)
        return WahaMessage(
            id=_message_id(data),
            chat_id=group_id,
            outcome="accepted" if isinstance(data, dict) else "unknown",
        )

    async def async_send_poll(
        self,
        recipient: str,
        question: str,
        options: list[str],
    ) -> WahaMessage:
        """Send a single-selection WhatsApp poll."""
        chat_id = normalize_chat_id(recipient)
        payload = {
            "session": self.session_name,
            "chatId": chat_id,
            "poll": {
                "name": question,
                "options": options,
                "multipleAnswers": False,
            },
        }
        data = await self._request("POST", "/api/sendPoll", json=payload)
        return WahaMessage(
            id=_message_id(data),
            chat_id=chat_id,
            outcome="accepted" if isinstance(data, dict) else "unknown",
        )

    async def async_resolve_lid(self, lid: str) -> str | None:
        """Resolve a WhatsApp LID to its phone-number direct-chat ID."""
        session = quote(self.session_name, safe="")
        encoded_lid = quote(lid, safe="")
        data = await self._request("GET", f"/api/{session}/lids/{encoded_lid}")
        if not isinstance(data, dict):
            raise WahaResponseError("WAHA returned invalid LID mapping data")

        phone_number = data.get("pn")
        if phone_number is None:
            return None
        if not isinstance(phone_number, str):
            raise WahaResponseError("WAHA returned an invalid LID phone number")
        try:
            return normalize_chat_id(phone_number)
        except ValueError as err:
            raise WahaResponseError(
                "WAHA returned an invalid LID phone number"
            ) from err

    async def async_create_group(self, name: str, participants: list[str]) -> str:
        """Create a group once; callers must persist its ID before further writes.

        A timeout has an unknown outcome and must not be retried automatically.
        """
        if not name.strip() or not participants:
            raise ValueError("Group name and initial participants are required")
        data = await self._request(
            "POST",
            f"/api/{quote(self.session_name, safe='')}/groups",
            json={"name": name, "participants": _participant_body(participants)},
        )
        # The GOWS engine in WAHA 2026.9.1 returns the raw Go GroupInfo
        # (capitalized ``JID``), while other engines may return ``id``.
        # The create endpoint has no documented response schema, so accept
        # only these exact, validated identifiers and never search by name.
        group_id = data.get("JID", data.get("id")) if isinstance(data, dict) else None
        if not _valid_group_id(group_id):
            raise WahaResponseError("WAHA did not return a valid group ID")
        return group_id

    async def async_get_group(self, group_id: str) -> dict[str, Any]:
        """Read the exact saved group, without searching by its mutable name."""
        data = await self._request("GET", self._group_path(group_id))
        returned_id = (
            data.get("JID", data.get("id")) if isinstance(data, dict) else None
        )
        if returned_id != group_id:
            raise WahaResponseError("WAHA returned invalid group information")
        return data

    async def async_get_group_participants(
        self, group_id: str
    ) -> list[WahaGroupParticipant]:
        """Read the cross-engine roster, retaining LID and verified PN separately."""
        data = await self._request(
            "GET", f"{self._group_path(group_id)}/participants/v2"
        )
        if not isinstance(data, list):
            raise WahaResponseError("WAHA returned an invalid group roster")
        participants = []
        for item in data:
            if not isinstance(item, dict):
                raise WahaResponseError("WAHA returned an invalid group participant")
            participant_id = item.get("id")
            role = item.get("role")
            pn = item.get("pn")
            if (
                not _valid_participant_id(participant_id)
                or role not in ("left", "participant", "admin", "superadmin")
                or (pn is not None and not _valid_participant_id(pn, pn_only=True))
            ):
                raise WahaResponseError("WAHA returned an invalid group participant")
            participants.append(WahaGroupParticipant(participant_id, role, pn))
        return participants

    async def async_add_group_participants(
        self, group_id: str, participants: list[str]
    ) -> None:
        """Request addition; caller must confirm each member in a fresh roster."""
        data = await self._request(
            "POST",
            f"{self._group_path(group_id)}/participants/add",
            json={"participants": _participant_body(participants)},
        )
        if _explicit_write_failure(data):
            raise WahaResponseError("WAHA did not add group participants")

    async def async_promote_group_admins(
        self, group_id: str, participants: list[str]
    ) -> None:
        """Request promotion; caller must confirm every role in a fresh roster."""
        data = await self._request(
            "POST",
            f"{self._group_path(group_id)}/admin/promote",
            json={"participants": _participant_body(participants)},
        )
        if _explicit_write_failure(data):
            raise WahaResponseError("WAHA did not promote group admins")

    async def async_get_group_security(self, group_id: str) -> dict[str, bool]:
        """Read all membership safeguards and message permissions."""
        path = f"{self._group_path(group_id)}/settings/security"
        fields = {
            "info_admin_only": ("info-admin-only", "adminsOnly"),
            "messages_admin_only": ("messages-admin-only", "adminsOnly"),
            "members_can_add": ("member-add-mode", "membersCanAddNewMember"),
            "membership_approval_required": (
                "membership-approval",
                "newMembersApprovalRequired",
            ),
        }
        result = {}
        for name, (suffix, field) in fields.items():
            data = await self._request("GET", f"{path}/{suffix}")
            if not isinstance(data, dict) or type(data.get(field)) is not bool:
                raise WahaResponseError(f"WAHA returned invalid {suffix} group setting")
            result[name] = data[field]
        return result

    async def async_set_group_security(
        self,
        group_id: str,
        *,
        info_admin_only: bool,
        messages_admin_only: bool,
        members_can_add: bool,
        membership_approval_required: bool,
    ) -> None:
        """Set all four controls; caller must read them back before readiness."""
        path = f"{self._group_path(group_id)}/settings/security"
        settings = (
            ("info-admin-only", "adminsOnly", info_admin_only),
            ("messages-admin-only", "adminsOnly", messages_admin_only),
            ("member-add-mode", "membersCanAddNewMember", members_can_add),
            (
                "membership-approval",
                "newMembersApprovalRequired",
                membership_approval_required,
            ),
        )
        for suffix, field, value in settings:
            if type(value) is not bool:
                raise ValueError(f"{field} must be a boolean")
            data = await self._request("PUT", f"{path}/{suffix}", json={field: value})
            if _explicit_write_failure(data):
                raise WahaResponseError(f"WAHA did not update {suffix} group setting")

    def _group_path(self, group_id: str) -> str:
        """Encode the exact group JID as one URL path component."""
        if not _valid_group_id(group_id):
            raise ValueError("Invalid WAHA group ID")
        return (
            f"/api/{quote(self.session_name, safe='')}/groups/"
            f"{quote(group_id, safe='')}"
        )

    async def async_ensure_webhook(
        self, url: str, hmac_key: str, *, group_events: bool = False
    ) -> bool:
        """Add or update our channel webhook without changing unrelated webhooks."""
        session_path = f"/api/sessions/{quote(self.session_name, safe='')}"
        data = await self._request("GET", session_path)
        if not isinstance(data, dict):
            raise WahaResponseError("WAHA returned invalid session configuration")

        raw_config = data.get("config")
        if raw_config is None:
            config: dict[str, Any] = {}
        elif isinstance(raw_config, dict):
            config = {**raw_config}
        else:
            raise WahaResponseError("WAHA returned invalid session configuration")

        raw_webhooks = config.get("webhooks", [])
        if not isinstance(raw_webhooks, list) or not all(
            isinstance(item, dict) for item in raw_webhooks
        ):
            raise WahaResponseError("WAHA returned invalid webhook configuration")

        events = ["poll.vote", "poll.vote.failed", "message", "message.reaction"]
        if group_events:
            events.extend(
                [
                    "group.v2.join",
                    "group.v2.leave",
                    "group.v2.participants",
                    "group.v2.participants.join-request",
                    "group.v2.update",
                ]
            )
        desired = {
            "url": url,
            "events": events,
            "hmac": {"key": hmac_key},
            "retries": {"policy": "constant", "delaySeconds": 1, "attempts": 3},
        }
        matching = [item for item in raw_webhooks if item.get("url") == url]
        if len(matching) == 1 and _webhook_matches(matching[0], desired):
            return False

        if matching:
            desired = {**matching[-1], **desired}
        webhooks = [item for item in raw_webhooks if item.get("url") != url]
        webhooks.append(desired)

        config["webhooks"] = webhooks
        await self._request(
            "PUT",
            session_path,
            json={"name": self.session_name, "config": config},
        )
        return True

    async def _request(
        self,
        method: str,
        path: str,
        *,
        json: dict[str, Any] | None = None,
        params: dict[str, str] | None = None,
    ) -> Any:
        """Perform one authenticated WAHA request."""
        headers = {"X-Api-Key": self._api_key, "Accept": "application/json"}
        if json is not None:
            headers["Content-Type"] = "application/json"

        try:
            async with asyncio.timeout(REQUEST_TIMEOUT_SECONDS):
                async with self._session.request(
                    method,
                    f"{self.base_url}/{path.lstrip('/')}",
                    headers=headers,
                    json=json,
                    params=params,
                ) as response:
                    try:
                        data = await response.json(content_type=None)
                    except TypeError, ValueError:
                        data = await response.text()
                    status = response.status
        except TimeoutError as err:
            raise WahaConnectionError("The request to WAHA timed out") from err
        except (ClientError, OSError) as err:
            raise WahaConnectionError(f"Unable to connect to WAHA: {err}") from err

        if status < 400:
            return data

        message = _error_message(data, status)
        if status in (401, 403):
            raise WahaAuthenticationError(message)
        raise WahaRequestError(message, status)


def _message_id(data: Any) -> str | None:
    """Extract a message ID across WAHA engine response variants."""
    if not isinstance(data, dict):
        return None
    direct_id = data.get("id")
    if isinstance(direct_id, str):
        return direct_id
    key = data.get("key")
    if isinstance(key, dict) and isinstance(key.get("id"), str):
        return key["id"]
    raw = data.get("_data")
    if isinstance(raw, dict):
        raw_id = raw.get("id")
        if isinstance(raw_id, dict) and isinstance(raw_id.get("id"), str):
            return raw_id["id"]
    return None


def _valid_group_id(value: Any) -> bool:
    """Accept numeric and legacy numeric-hyphen group JIDs, not paths."""
    if not isinstance(value, str) or not value.endswith("@g.us"):
        return False
    local = value.removesuffix("@g.us")
    segments = local.split("-")
    return len(segments) in (1, 2) and all(
        1 <= len(segment) <= 20 and segment.isascii() and segment.isdigit()
        for segment in segments
    )


def _explicit_write_failure(data: Any) -> bool:
    """Detect WAHA's explicit failed-write shapes; roster remains authoritative."""
    return data is False or (isinstance(data, dict) and data.get("success") is False)


def _valid_participant_id(value: Any, *, pn_only: bool = False) -> bool:
    """Accept WAHA phone-number and LID account JIDs."""
    if not isinstance(value, str):
        return False
    suffixes = ("@c.us",) if pn_only else ("@c.us", "@lid")
    return any(
        value.endswith(suffix)
        and 1 <= len(value.removesuffix(suffix)) <= 20
        and value.removesuffix(suffix).isascii()
        and value.removesuffix(suffix).isdigit()
        for suffix in suffixes
    )


def _participant_body(participants: list[str]) -> list[dict[str, str]]:
    """Validate a non-empty, unambiguous batch before an external write."""
    if not participants or len(participants) != len(set(participants)):
        raise ValueError("Group participants must be non-empty and unique")
    if not all(_valid_participant_id(item) for item in participants):
        raise ValueError("Invalid WAHA participant ID")
    return [{"id": item} for item in participants]


def _webhook_matches(current: dict[str, Any], desired: dict[str, Any]) -> bool:
    """Compare integration-owned fields while allowing WAHA response defaults."""
    return all(current.get(key) == value for key, value in desired.items())


def _error_message(data: Any, status: int) -> str:
    """Extract a useful, non-secret error message."""
    if isinstance(data, dict):
        for key in ("message", "error"):
            value = data.get(key)
            if isinstance(value, str) and value:
                return value
            if isinstance(value, dict):
                message = value.get("message")
                if isinstance(message, str) and message:
                    return message
    return f"WAHA request failed with HTTP {status}"


def _optional_string(value: Any) -> str | None:
    """Return a value only when it is a string."""
    return value if isinstance(value, str) else None
