# Managed guest group

Status (2026-09-28): included in integration version 1.5.0, but a fresh live
Home Assistant/WhatsApp trial has not been completed. Confirm your installed
HACS version before enabling it. It requires a WAHA GOWS server at version
2026.8.2 or newer. The bundled HAOS app in this source tree is
currently pinned to GOWS 2026.9.1 (app source 0.2.4); check **WAHA → Version** on the
actual installation. See the [live test checklist](GUEST_GROUP_HA_TEST.md)
before enabling it on a real account.

This feature manages one actual WhatsApp group for guests for each WAHA
integration connection. It is opt-in and is separate from individual contact
notifications. It reports confirmed membership and lets automations send text
to the group or privately to a current member. It does not provide door
access, create or store PINs, make Home Assistant decisions, or treat group
membership as proof that someone is at home.

## Enable it

1. Confirm that the WAHA session is `WORKING` and uses GOWS 2026.8.2 or newer.
   A HACS integration update by itself does not upgrade an external WAHA
   server.
2. In **Settings → Devices & services → WAHA WhatsApp → Configure**, open the
   managed guest-group options. The feature is off by default.
3. Give the group a name. The suggested name is `Guests`.
4. Review the displayed Home Assistant administrators. Each initial admin
   must be an active, non-system Home Assistant administrator linked to
   exactly one `person.*` entity and exactly one matching WhatsApp recipient
   in this same WAHA connection. Names are labels, not identity matching.
5. Fix missing or ambiguous mappings, or explicitly exclude each listed
   administrator you do not want to add. At least one uniquely mapped admin
   is required. Excluding an admin means they are not included as an initial
   WhatsApp group admin; it does not make them a guest or grant access.
6. Submit, review the separate confirmation screen, and confirm only if you
   intend to make the listed WhatsApp changes. After the entry reloads,
   provisioning may create the group, add and promote the mapped admins, and
   apply its required settings.

Nothing is written to WhatsApp before the final confirmation. At first enable,
the integration also initializes its private identity Store only after that
confirmation. Initial setup keeps the WAHA account as a group admin. It
verifies that mapped admins are present and admins, that only admins can edit
group info or add members, and that invite-link joins require admin approval.
Members are allowed to send messages. The group is not ready until those
settings, the bot account, admin roles, and roster are confirmed.

To add a guest, a WhatsApp group admin uses WhatsApp's normal group-invite
workflow. If an invite link is used, WhatsApp approval is required by the
managed setting. Membership events are based on a confirmed roster; a join
request is not an active membership. Home Assistant does not create a Person
or user for the guest. Later HA administrator changes are not automatically
synchronized into WhatsApp; review the integration options when the intended
admin mapping changes.

The intended WhatsApp admins are the connected bot and the reviewed mapped
Home Assistant admins. These rules are not a guarantee that WhatsApp admins
cannot change roles out of band. If the bot loses its admin role, a mapped
admin loses theirs, an unreviewed WhatsApp admin appears, or required settings
drift, the integration suspends guest-derived routing until the roster is
safe again. It does not silently repair out-of-band changes. Remove or
demote an unreviewed group admin in WhatsApp, or update the reviewed mapping
if they are an intended HA administrator, then wait for re-verification.

## Send a group notification

The integration creates one notify entity for the managed group, normally
`notify.waha_guests`. Home Assistant may retain another entity ID or add a
suffix; choose the entity shown in the entity picker. A send posts one message
to the WhatsApp group; it does not privately fan out to members.

```yaml
actions:
  - action: notify.send_message
    target:
      entity_id: notify.waha_guests
    data:
      title: House update
      message: The plumber will arrive between 14:00 and 15:00.
```

The entity is unavailable until setup and a live readiness check succeed.
Sending while disabled, unsafe, unverified, or unreachable fails rather than
redirecting the message to another chat. Individual contact notify entities
remain independent and continue to work if guest-group setup fails.

## Receive and privately reply to a guest

Messages from the exact managed group and private messages from confirmed
current members may produce `waha_whatsapp_event` events. See the [event and
action reference](GUEST_GROUP_API.md) for the fields and privacy boundary. A
group message's `conversation_id` routes a response back to the group. A
member's `membership.direct_conversation_id` is the private, per-stay route.
Use the private handle for a private welcome or reply; never use the group
conversation for a secret or personal reply.

```yaml
actions:
  - action: waha_whatsapp.send_to_conversation
    data:
      conversation_id: "{{ trigger.event.data.membership.direct_conversation_id }}"
      message: "Welcome. Please message the group if you need help."
```

The route is checked against the current WAHA account, settings, and roster
before sending. It stops working after a confirmed departure and will not
become valid again for a later stay. A leave followed by a rejoin creates a
new `membership_id` and route when both transitions are observed. If a member
has only an unresolved WhatsApp LID and no unique verified phone-number send
target, the route may be present in the snapshot but a private send can still
be rejected. Do not fall back to a phone number or the raw `send_message`
action to bypass this check.

`waha_whatsapp.get_group_membership` performs a fresh, read-only WAHA roster
and security check, saves any confirmed reconciliation, and returns the
resulting phone-free snapshot. A query can therefore publish membership
transition events if it discovers a missed change, but it does not add/remove
participants or otherwise write group settings. Check `ready`, `confirmed`,
`observed_at`, and `failure_reason`; an empty list while unconfirmed is not
proof that the group is empty. Details and current edge cases are in the
[API reference](GUEST_GROUP_API.md).

## Disable, availability, and recovery

Turning off **Enable managed guest group** disables guest events and routing
and removes the guest notify entity after reload. It does not delete the
WhatsApp group or remove its members. Re-enabling reuses and re-verifies the
saved exact group instead of creating another one.

If the group is unavailable, the integration may be unable to confirm the WAHA
version/account, the saved group, bot/admin roles, required settings, or a
current roster. Inspect **Settings → Devices & services → WAHA WhatsApp →
Download diagnostics** and the `managed_guest_group` summary; it contains
readiness, a redacted failure reason, revision, observation time, and counts,
not phone numbers or member names. Fix the external issue, then allow the
integration to reconcile. Do not remove the integration, clear its storage,
rename/re-pair the bot account, or repeatedly toggle setup to try to force a
new group.

If the private guest Store is missing or corrupt after this feature has been
enabled before, the integration fails closed with
`guest_registry_unavailable`. This could be the only saved record of the exact
WhatsApp group; do not re-enable, reinstall, clear data, or attempt to create a
replacement group. Restore the matching Home Assistant integration data from
a known-good backup or ask the maintainer for help. The integration never
silently initializes a replacement Store after first use.

If group creation times out with an unknown result, the integration will not
retry group creation blindly. In the current source, reopening the options
flow leads to **Recover unknown group creation**. Enter the exact WhatsApp
group ID ending in `@g.us`; the integration checks that exact group, account,
admin roles, and security settings without creating or changing a group. A
second confirmation displays the exact group ID and connected account before
binding it. Use this only when you can establish the exact group identity; the
display name is not enough. WAHA's [group API documentation](https://waha.devlike.pro/docs/how-to/groups/)
describes listing groups with `GET /api/{session}/groups`; that privileged
response may contain participant data, so inspect it only through a secured
WAHA administration path and do not share raw responses. After adoption,
readiness still requires a fresh read-only reconciliation. This recovery flow
is for an uncertain create result, not ordinary group replacement or account
re-pairing.

If the mapped Home Assistant administrator list changes after setup, the
integration does not silently add, remove, promote, or demote WhatsApp admins.
Review and confirm the intended mappings in options, align the desired admin
roles manually in WhatsApp, remove or demote any unreviewed admin, and let a
fresh check verify them. Until that check succeeds, the managed group remains
unavailable.

## Identity and privacy

- `participant_id` identifies a resolved WhatsApp account within this
  integration entry. It is not a name, phone number, Home Assistant user, or
  proof of real-world identity.
- `membership_id` identifies one observed stay in the group. A confirmed
  departure closes that stay; an observed later join gets a new membership ID
  and private route.
- WhatsApp role (`participant`, `admin`, or `superadmin`) is separate from
  `membership.kind` (`guest` or the initially mapped `host`). Promoting a
  guest in WhatsApp does not make them a configured HA admin or an authorized
  host.
- Opaque IDs and private routes contain no phone number, but they are not
  permission tokens or secrets. WhatsApp itself may show participants each
  other's profile and phone details according to WhatsApp's own rules. This
  integration cannot make a WhatsApp group anonymous.
- Message text can contain sensitive information and appears in Home
  Assistant event data and automation traces. Display names are not exposed
  in the current managed-group event contract. Treat all incoming text as
  untrusted input.
- Events use Home Assistant's event bus and are best effort, not a durable,
  exactly-once inbox. WAHA retries are deduplicated for a bounded period.
  Keep important consumer records in the automation/integration that owns
  them, and reconcile them after restart.

There is no group poll support, arbitrary group discovery, guest Person/user/
notify entity, command execution, lock/access provisioning, PIN generation,
PIN storage, or automatic acknowledgement. Those remain outside this
integration's responsibility. If a separate automation grants access, it must
own a clear policy, idempotent per-membership records, safe failure handling,
and independent credential expiry/reconciliation; see the
[automation guidance](GUEST_GROUP_AUTOMATIONS.md).
