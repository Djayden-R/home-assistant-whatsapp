"""Explicit, read-only general group routing, independent of guest lifecycle."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from .const import SESSION_STATUS_WORKING

if TYPE_CHECKING:
    from homeassistant.config_entries import ConfigEntry
    from homeassistant.core import HomeAssistant

    from .api import WahaClient, WahaMessage
    from .channel_registry import ChannelRegistry
    from .guest_registry import GuestRegistry

SUBENTRY_TYPE_GROUP = "group"
CONF_GROUP_ID = "group_id"
CONF_GROUP_ENABLED = "enabled"
CONF_GROUP_ACCOUNT = "bot_account"
_GROUP_ID = re.compile(r"^[0-9]{1,20}(?:-[0-9]{1,20})?@g\.us$")
_PARTICIPANT = re.compile(r"^[0-9]{1,20}@(c\.us|s\.whatsapp\.net|lid)$")
MIN_GOWS_VERSION = (2026, 8, 2)


class GeneralGroupError(ValueError):
    """A sanitized group eligibility failure, with no provider detail."""

    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(reason)


def normalize_group_id(value: Any) -> str:
    """Accept an explicit group JID or its numeric (possibly legacy) local ID."""
    if not isinstance(value, str):
        raise GeneralGroupError("invalid_group_id")
    value = value.strip().lower()
    if "@" not in value:
        value += "@g.us"
    if not _GROUP_ID.fullmatch(value):
        raise GeneralGroupError("invalid_group_id")
    return value


def group_id_matches(value: Any, group_jid: str) -> bool:
    """Compare configured IDs canonically, ignoring malformed saved values."""
    try:
        return normalize_group_id(value) == group_jid
    except GeneralGroupError:
        return False


def participant_alias(value: Any) -> str | None:
    """Canonicalize known network JIDs without inferring PN/LID relationships."""
    if not isinstance(value, str) or not _PARTICIPANT.fullmatch(value.lower()):
        return None
    return value.lower().replace("@s.whatsapp.net", "@c.us")


@dataclass(frozen=True, slots=True)
class GeneralGroupDestination:
    """One enabled, account-bound configured destination; raw IDs stay private."""

    conversation_id: str
    subentry_id: str
    group_jid: str
    bot_account: str


@dataclass(frozen=True, slots=True)
class VerifiedGeneralGroup:
    """Fresh roster proof from the exact connected account and exact group."""

    group_jid: str
    bot_account: str
    # Every alias maps to one canonical identity. Never include display names.
    aliases: dict[str, str]
    bot_identity: str

    def identity(self, alias: Any) -> str | None:
        normalized = participant_alias(alias)
        return self.aliases.get(normalized) if normalized else None


class GeneralGroupManager:
    """Resolve explicit destinations and reverify eligibility before each send."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        client: WahaClient,
        channel_registry: ChannelRegistry,
        guest_registry: GuestRegistry | None = None,
    ) -> None:
        self._entry = entry
        self._client = client
        self._channel = channel_registry
        self._guests = guest_registry

    def _destination(self, subentry: Any) -> GeneralGroupDestination | None:
        if (
            subentry.subentry_type != SUBENTRY_TYPE_GROUP
            or subentry.data.get(CONF_GROUP_ENABLED) is not True
        ):
            return None
        try:
            group = normalize_group_id(subentry.data.get(CONF_GROUP_ID))
        except GeneralGroupError:
            return None
        account = participant_alias(subentry.data.get(CONF_GROUP_ACCOUNT))
        if account is None or self._is_guest_group(group):
            return None
        token = self._channel.opaque_id(
            "general_group_conversation",
            f"{subentry.subentry_id}\0{group}\0{account}",
            "general_group",
        )
        return GeneralGroupDestination(token, subentry.subentry_id, group, account)

    def _is_guest_group(self, group: str) -> bool:
        return self._guests is not None and self._guests.saved_group_jid == group

    def resolve_subentry(self, subentry_id: str) -> GeneralGroupDestination | None:
        """Return a unique enabled configuration, checking corrupt duplicates."""
        subentry = self._entry.subentries.get(subentry_id)
        destination = self._destination(subentry) if subentry is not None else None
        if destination is None:
            return None
        duplicates = sum(
            1
            for candidate in self._entry.subentries.values()
            if candidate.subentry_type == SUBENTRY_TYPE_GROUP
            and group_id_matches(
                candidate.data.get(CONF_GROUP_ID), destination.group_jid
            )
        )
        return destination if duplicates == 1 else None

    def resolve_conversation(
        self, conversation_id: str
    ) -> GeneralGroupDestination | None:
        """Removed, disabled, or changed destinations immediately lose their routes."""
        return next(
            (
                destination
                for subentry_id in self._entry.subentries
                if (destination := self.resolve_subentry(subentry_id)) is not None
                and destination.conversation_id == conversation_id
            ),
            None,
        )

    def resolve_group(self, group_jid: str) -> GeneralGroupDestination | None:
        """Match one explicitly configured group, never discover account chats."""
        return next(
            (
                destination
                for subentry_id in self._entry.subentries
                if (destination := self.resolve_subentry(subentry_id)) is not None
                and destination.group_jid == group_jid
            ),
            None,
        )

    def conversation_destination(self, conversation_id: str) -> str | None:
        """Supply currently eligible private quote ownership to ChannelRegistry."""
        destination = self.resolve_conversation(conversation_id)
        return destination.group_jid if destination else None

    def participant_id(
        self, destination: GeneralGroupDestination, identity: str
    ) -> str:
        """Scope participant identity to this account and group."""
        return self._channel.opaque_id(
            "general_group_participant",
            f"{destination.bot_account}\0{destination.group_jid}\0{identity}",
            "participant",
        )

    async def async_review(self, group_input: str) -> VerifiedGeneralGroup:
        """Read-only configuration proof; no group creation or role/security changes."""
        group = normalize_group_id(group_input)
        if self._is_guest_group(group):
            raise GeneralGroupError("managed_group_reserved")
        server = await self._client.async_get_server()
        version = re.fullmatch(
            r"(\d{4})\.(\d{1,2})\.(\d+)(?:\+[0-9A-Za-z.-]+)?", server.version
        )
        if (
            not isinstance(server.engine, str)
            or server.engine.upper() != "GOWS"
            or version is None
            or tuple(map(int, version.groups())) < MIN_GOWS_VERSION
        ):
            raise GeneralGroupError("unsupported_waha_capabilities")
        session = await self._client.async_get_session()
        account = participant_alias(session.account_id)
        if session.status != SESSION_STATUS_WORKING or account is None:
            raise GeneralGroupError("unverified_bot_account")
        await self._client.async_get_group(group)
        roster = await self._client.async_get_group_participants(group)
        aliases: dict[str, str] = {}
        for member in roster:
            if member.role == "left":
                continue
            member_id = participant_alias(member.id)
            pn = participant_alias(member.pn) if member.pn is not None else None
            if member_id is None or (
                member.pn is not None and (pn is None or not pn.endswith("@c.us"))
            ):
                raise GeneralGroupError("unverified_group_membership")
            if pn and member_id.endswith("@c.us") and member_id != pn:
                raise GeneralGroupError("ambiguous_group_identity")
            identity = pn or member_id
            for alias in {member_id, pn} - {None}:
                if alias in aliases:
                    raise GeneralGroupError("ambiguous_group_identity")
                aliases[alias] = identity
        bot = aliases.get(account)
        if bot is None:
            raise GeneralGroupError("unverified_group_membership")
        return VerifiedGeneralGroup(group, account, aliases, bot)

    async def async_verify(
        self, destination: GeneralGroupDestination
    ) -> VerifiedGeneralGroup:
        """Check saved route and account binding again around fresh roster I/O."""
        if self.resolve_conversation(destination.conversation_id) != destination:
            raise GeneralGroupError("invalid_conversation")
        proof = await self.async_review(destination.group_jid)
        if proof.bot_account != destination.bot_account:
            raise GeneralGroupError("bot_account_changed")
        if self.resolve_conversation(destination.conversation_id) != destination:
            raise GeneralGroupError("invalid_conversation")
        return proof

    async def async_send_text(
        self, conversation_id: str, text: str, *, reply_to: str | None = None
    ) -> WahaMessage:
        """Send after fresh verification; the common caller owns observation."""
        destination = self.resolve_conversation(conversation_id)
        if destination is None:
            raise GeneralGroupError("invalid_conversation")
        await self.async_verify(destination)
        return await self._client.async_send_group_text(
            destination.group_jid, text, reply_to_message_id=reply_to
        )
