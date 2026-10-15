# Managed guest-group event and action API

This reference describes the managed-group contract in integration version
1.5.0. Live WAHA behavior has not yet been validated. See the
[user guide](GUEST_GROUPS.md), [automation guide](GUEST_GROUP_AUTOMATIONS.md),
and [manual live test](GUEST_GROUP_HA_TEST.md).

The managed-group events extend the existing `waha_whatsapp_event` event bus
with `schema_version: 1`. IDs below are examples only. Public participant,
membership, group, route, event, and message IDs are opaque integration IDs;
they do not encode WhatsApp phone numbers or raw JIDs. The event surfaces do
not include a guest display name, phone number, raw JID, WAHA webhook secret,
complete WAHA payload, media URL, or generated PIN.

WAHA callbacks must pass the integration's SHA-512 webhook signature check and
session/group validation before they are considered. These identifiers are
communication metadata, not authentication tokens.

The [1.7.0 communication API](CHANNEL_API.md) adds optional outbound
observations and correlated sends. Explicit general groups have a separate
contract and do not gain guest lifecycle or private participant routes.

## Identity and stay lifecycle

| Field | Identifies | Stability and use |
| --- | --- | --- |
| `participant_id` | A resolved WhatsApp account within this integration entry | Reused while the private identity record and verified aliases remain. Use for account-level association, not as a name or proof of a real-world person. |
| `membership_id` | One observed continuous stay in the exact managed group | A confirmed leave closes it; a later observed join creates a new ID. Use it as the key for per-stay consumer records. |
| `group_id` | The exact managed group | Stable across restart and display-name changes while saved; changes only after reviewed replacement/recovery. |
| `conversation_id` | Where the enclosing event occurred | On a group event, replies to the group. On an unconfigured guest DM, it is that stay's private route. It is never an identity. |
| `membership.direct_conversation_id` | A current member's private route | Available only on active membership records; it becomes invalid after departure and is not reused for a later stay. |

The integration retains at most 512 closed memberships and removes closed
records older than 180 days. Active memberships are not pruned to meet that
limit. A targeted lookup outside retained history returns
`membership_found: false`; a consumer that needs longer PIN/access/task history
must retain its own durable per-membership record. Integration deletion,
storage loss, or ambiguous PN/LID identity can break continuity. IDs are not
globally permanent.

`membership.kind` is `host` for a member matched to the reviewed initial
Home Assistant administrator mapping; otherwise it is `guest`.
`membership.whatsapp_role` is one of `participant`, `admin`, or `superadmin`.
These describe different dimensions. Promotion to WhatsApp admin does not
change guest/host classification, create a Home Assistant user, or grant any
house-action permission.

## Membership lifecycle events

Confirmed changes use `participant`, not `sender`, because the event is about
the affected group member:

```yaml
event_type: waha_whatsapp_event
data:
  schema_version: 1
  event_id: evt_membership_example
  type: group.participant.joined
  config_entry_id: entry_example
  conversation_id: group_conversation_example
  conversation_type: group
  occurred_at: "2026-09-28T12:00:00+00:00"
  observed_at: "2026-09-28T12:00:01+00:00"
  source: webhook
  group:
    group_id: group_example
    purpose: guests
  participant:
    participant_id: participant_example
  membership:
    participant_id: participant_example
    membership_id: membership_example
    kind: guest
    status: active
    whatsapp_role: participant
    direct_conversation_id: guest_direct_example
```

`group.participant.left` has the same envelope and affected IDs, with
`membership.status: left`; a closed membership has no
`direct_conversation_id`. `group.participant.role_changed` carries the
current membership and role. There is no `actor` field; the group webhook
does not reliably establish who initiated every change. These events never
attribute a join/leave action to the affected participant as a Home Assistant
user.

For example, the same membership after a confirmed leave is:

```yaml
event_type: waha_whatsapp_event
data:
  schema_version: 1
  event_id: evt_membership_left_example
  type: group.participant.left
  config_entry_id: entry_example
  conversation_id: group_conversation_example
  conversation_type: group
  occurred_at: "2026-09-28T13:00:00+00:00"
  observed_at: "2026-09-28T13:00:02+00:00"
  source: webhook
  group:
    group_id: group_example
    purpose: guests
  participant:
    participant_id: participant_example
  membership:
    participant_id: participant_example
    membership_id: membership_example
    kind: guest
    status: left
    whatsapp_role: participant
```

A WhatsApp admin promotion changes only the WhatsApp role, not the
classification or stay identity:

```yaml
event_type: waha_whatsapp_event
data:
  schema_version: 1
  event_id: evt_membership_role_example
  type: group.participant.role_changed
  config_entry_id: entry_example
  conversation_id: group_conversation_example
  conversation_type: group
  occurred_at: "2026-09-28T12:30:00+00:00"
  observed_at: "2026-09-28T12:30:01+00:00"
  source: webhook
  group:
    group_id: group_example
    purpose: guests
  participant:
    participant_id: participant_example
  membership:
    participant_id: participant_example
    membership_id: membership_example
    kind: guest
    status: active
    whatsapp_role: admin
    direct_conversation_id: guest_direct_example
```

`source` is `webhook` when a matching, authenticated participant-change hint
supplied a trustworthy transition time; otherwise it is `reconciliation`.
`occurred_at` is an ISO timestamp or `null` when a snapshot established the
change but its actual time is unknown. `observed_at` is when the roster was
confirmed. A change found while HA was offline must not be assigned a made-up
occurrence time.

The integration emits `group.membership.reconciled` as a snapshot signal, not
as a join or leave. It has the shared event envelope and group metadata but
no `participant` or `membership` object; `occurred_at` is `null` and
`source` is `reconciliation`. The first provisioning snapshot is a baseline
and does not generate fake historical join events. Query the current roster
to reconcile after this event.

```yaml
event_type: waha_whatsapp_event
data:
  schema_version: 1
  event_id: evt_snapshot_example
  type: group.membership.reconciled
  config_entry_id: entry_example
  conversation_id: group_conversation_example
  conversation_type: group
  occurred_at: null
  observed_at: "2026-09-28T12:00:01+00:00"
  source: reconciliation
  group:
    group_id: group_example
    purpose: guests
```

## Guest messages and reactions

A text sent in the exact managed group is represented by the established
`message.received` type. The group's `conversation_id` is the group destination;
the actual sender is nested separately:

```yaml
event_type: waha_whatsapp_event
data:
  schema_version: 1
  event_id: evt_group_message_example
  type: message.received
  config_entry_id: entry_example
  conversation_id: group_conversation_example
  conversation_type: group
  occurred_at: "2026-09-28T12:02:00+00:00"
  group:
    group_id: group_example
    purpose: guests
  sender:
    participant_id: participant_example
    direct_conversation_id: guest_direct_example
  membership:
    membership_id: membership_example
    kind: guest
    status: active
    whatsapp_role: participant
    direct_conversation_id: guest_direct_example
  message:
    id: msg_example
    kind: text
    text: "The delivery has arrived."
    in_reply_to: null
```

For a private DM from a member who is not already a configured contact,
`conversation_type` is `direct` and `conversation_id` is the same active
membership's private route:

```yaml
event_type: waha_whatsapp_event
data:
  schema_version: 1
  event_id: evt_private_message_example
  type: message.received
  config_entry_id: entry_example
  conversation_id: guest_direct_example
  conversation_type: direct
  occurred_at: "2026-09-28T12:03:00+00:00"
  group:
    group_id: group_example
    purpose: guests
  sender:
    participant_id: participant_example
    direct_conversation_id: guest_direct_example
  membership:
    membership_id: membership_example
    kind: guest
    status: active
    whatsapp_role: participant
    direct_conversation_id: guest_direct_example
  message:
    id: msg_private_example
    kind: text
    text: "Could you send the arrival details privately?"
    in_reply_to: null
```

Configured contacts keep their pre-existing direct conversation and sender
metadata. If a configured contact is also a verified group member, the
existing direct event may gain `sender.participant_id`, `group`, and
`membership` as additive fields after time/identity checks; it is not emitted
twice and does not replace that contact's stable direct `conversation_id`.
Group messages from such a member may additionally include that configured
contact's existing `sender.contact_id`, `sender.notify_entity_id`, and
`sender.person_entity_id` when the same account is unambiguously verified.

Reactions use `type: reaction.added` or `reaction.removed` and include:

```yaml
event_type: waha_whatsapp_event
data:
  schema_version: 1
  event_id: evt_reaction_example
  type: reaction.added
  config_entry_id: entry_example
  conversation_id: group_conversation_example
  conversation_type: group
  occurred_at: "2026-09-28T12:04:00+00:00"
  group:
    group_id: group_example
    purpose: guests
  sender:
    participant_id: participant_example
    direct_conversation_id: guest_direct_example
  membership:
    membership_id: membership_example
    kind: guest
    status: active
    whatsapp_role: participant
    direct_conversation_id: guest_direct_example
  reaction:
    emoji: "👍" # Empty string on removal
    target_message_id: msg_example
    target_known: true
```

Inbound guest messages/reactions require a known group/member and a trusted
message timestamp within the membership interval. Guest inbound content older
than one hour or more than five minutes in the future is discarded. Stale or
untrusted membership webhook timestamps can still trigger a roster refresh,
but cannot be used to manufacture historical event times. Unknown senders,
other groups, join requests, and conflicted/unresolved identities do not gain
guest routes. Incoming message text is user-controlled and may itself include
sensitive information; it can appear in event listeners and automation
traces.

For non-text messages, the existing content parser can emit a metadata-only
placeholder such as `message.kind: audio`, `image`, `file`, or `unknown`, with
an optional normalized `message.mime_type` and `message.caption`. It does not
download media or expose its URL or binary data.

## Query current membership

`waha_whatsapp.get_group_membership` requires `config_entry_id`; an optional
`membership_id` selects one active or retained closed stay. It returns a
Home Assistant service response, so automations can use `response_variable`:

```yaml
- action: waha_whatsapp.get_group_membership
  data:
    config_entry_id: YOUR_CONFIG_ENTRY_ID
    membership_id: membership_example # Omit for the full retained snapshot
  response_variable: guest_snapshot
```

Every query performs a fresh, read-only WAHA reconciliation of the saved
group, bot account, security settings, reviewed admin roles, and participant
roster. It does not create/replace the group, change settings, or add/remove
participants. If the read discovers a membership transition, it persists the
updated snapshot and can publish the corresponding membership event before
returning. If WAHA cannot verify a safe state, the response fails closed with
`ready: false`, `confirmed: false`, and a safe `failure_reason` where
available; it is not an empty confirmed group.

`confirmed` means the private Store loaded and this runtime has completed a
successful authoritative roster reconciliation. Restored state alone is not
confirmed. `ready` additionally means the feature is enabled, the exact group
is saved, required settings and admin roles are verified, and provisioning is
in the `ready` state. The query runs that fresh check before returning.

Example response for a known active stay:

```yaml
group_id: group_example
conversation_id: group_conversation_example
ready: true
confirmed: true
provisioning_status: ready
revision: 7
observed_at: 1790597100.0 # Unix timestamp in seconds; null before a roster is observed
failure_reason: null
membership_found: true
memberships:
  - participant_id: participant_example
    membership_id: membership_example
    kind: guest
    status: active
    whatsapp_role: participant
    direct_conversation_id: guest_direct_example
```

On a whole-group query, `memberships` contains active and retained closed
records and `membership_found` is omitted. On a targeted query,
`membership_found: true` means one active or retained closed record was found;
`false` means unknown or expired from retention. A found closed record has
`status: left` and no `direct_conversation_id`. `observed_at` is Unix epoch
seconds in the query response (membership events instead use ISO-8601
strings). Compare it with the consumer's freshness requirement; a snapshot is
not proof beyond its observation time and an external leave can race a send.

Possible `provisioning_status` values are `unconfigured`, `creating`,
`create_unknown`, `group_saved`, `ready`, and `suspended`. `failure_reason`
is null on success or a safe reason code such as
`unsupported_waha_capabilities`, `admin_mapping_changed`,
`unsafe_group_settings`, `bot_admin_unconfirmed`, `admin_role_drift`,
`bot_account_changed`, `group_create_outcome_unknown`, or
`unexpected_group_admin` or `guest_registry_unavailable`. Treat codes as
diagnostics, not user identity. `unexpected_group_admin` means an account
outside the connected bot and reviewed initial HA-admin mappings currently
has a WhatsApp admin role; the integration suspends guest routing until this
is resolved and the roster is reverified.
`guest_registry_unavailable` means private identity state could not be
trusted/restored; it does not authorize resetting state or creating a
replacement group.

## Send text to a group or member

The native group notify entity sends a single message to the group:

```yaml
- action: notify.send_message
  target:
    entity_id: notify.waha_guests
  data:
    title: House update
    message: The test is complete.
```

For event-driven replies or a private welcome, use
`waha_whatsapp.send_to_conversation`. It accepts a configured-contact route,
the managed group route, or a current guest private route. A private reply
quotes the member's private message in that same conversation:

```yaml
- action: waha_whatsapp.send_to_conversation
  data:
    conversation_id: "{{ trigger.event.data.membership.direct_conversation_id }}"
    message: "Thanks, I will check."
    reply_to_message_id: "{{ trigger.event.data.message.id }}"
```

For a group reply, select the event's top-level `conversation_id` and quote
that group's message:

```yaml
- action: waha_whatsapp.send_to_conversation
  data:
    conversation_id: "{{ trigger.event.data.conversation_id }}"
    message: "Thanks, I will check."
    reply_to_message_id: "{{ trigger.event.data.message.id }}"
```

Never combine a group event's quote ID with a private member route (or a
private message's quote ID with the group route); quote targets are scoped to
their own conversation. For a group event, `sender.direct_conversation_id` and
`membership.direct_conversation_id` identify the sender's private route.
The private-route send verifies the exact current roster destination before
sending, serialized with local membership changes. An external WhatsApp
removal racing that check/send cannot be made perfectly atomic. Invalid,
expired, disabled, stale, unresolved, or unsafe routes fail with a service
error; the integration does not silently redirect them.

The separate legacy `waha_whatsapp.send_message` action accepts an explicit
phone number and is an independently authorized outbound HA action. It is not
governed by managed guest membership. Do not use it as a fallback for a
rejected guest route or to bypass group-membership checks.

## Delivery and compatibility limits

Membership transitions are persisted before being published, but Home
Assistant event delivery is best effort, not a durable or exactly-once queue.
Consumers should use `event_id` for idempotency, persist their own records,
query snapshots after restart, and define behavior when a query is unavailable.
The private guest identity Store is separate from the existing channel
correlation Store; a failure of guest state suspends guest routes while
individual contacts continue independently.

Snapshots cannot detect a leave and rejoin that both happened while HA/WAHA
was offline. In that case they cannot prove a new stay boundary and may retain
the previous membership. Do not use this feature alone as a long-lived access
control signal. The integration creates no PINs, stores no lock credentials,
does not authorize commands, and does not infer physical presence.
