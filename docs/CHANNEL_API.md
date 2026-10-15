# WhatsApp communication API

Integration **1.7.0** adds these backward-compatible schema-v1 interfaces.
Source-level validation is complete; live Home Assistant/WhatsApp verification
remains required. This integration transports messages and inert command
requests. Consumers own execution policy, authorization, retention, inboxes,
and retries of their own work. There is no dependency on an agent runtime.

Existing [commands](COMMANDS.md), [managed guest-group API](GUEST_GROUP_API.md),
and [direct-channel design](WHATSAPP_CHANNEL_DESIGN.md) remain applicable.
General groups are independent configured destinations, with no provisioning,
guest stays, host classification, private participant routes, or role changes.
The public event bus is best effort and is not proof of authenticated origin
for code that subscribes to it. Other HA code can publish the same event name.

## Requirements and migration

- Home Assistant 2026.7.0 or newer, as declared by HACS; use a supported HA
  installation. Repository checks use Python 3.14.2 or newer.
- Existing direct text/poll paths retain their existing backend requirements.
  Outbound observation and correlated text sends add no backend endpoint.
- General groups and their mention parser require **WAHA GOWS 2026.8.2 or
  newer**, including builds with a `+haos` suffix. The exact group, connected
  account, and v2 roster must be verifiable. Other engines fail closed.
- No HAOS app runtime change or app version bump is required. The packaged app
  currently pins WAHA GOWS 2026.9.1, which meets the version gate.
- Existing contacts, poll actions, registered commands, and managed guest
  lifecycle remain unchanged. No group is discovered or enabled automatically.
  No deployment, account reconnection, or consumer activation is required to
  install the source update; perform live setup separately.

Configure **Enable outbound observations** in the connection options to publish
rendered send content. It defaults to false, independently of guest-group
options. A recipient or general group can instead enable **Redact observed
content**. Existing notify calls need no new parameters. The action-level
`content_redacted: true` additionally protects individual replies or polls.
The separate **Redact managed guest-group observed content** connection option
protects the native managed group notify path and its conversation replies.
Use those controls or a permanently redacted destination for credentials, PINs,
Wi-Fi passwords, and other sensitive messages. This integration does not
recognize secrets from text. Arbitrary-phone `send_message` calls are always
excluded from outbound observations, even when the phone matches a contact.
Do not substitute that legacy action for a rejected conversation route.

## Capability discovery

```yaml
action: waha_whatsapp.get_channel_capabilities
data:
  config_entry_id: entry_example
response_variable: channel_capabilities
```

The response reports this active connection, rather than all features present
in source. It reads session/server state and revalidates a configured general
group before reporting group capability:

```json
{
  "schema_versions": [1],
  "outbound_observations": true,
  "correlated_sends": true,
  "general_groups": true,
  "mentions": true,
  "delivery_receipts": false,
  "message_changes": false,
  "media_retrieval": false,
  "transport_replay": false,
  "manual_outbound_observations": false,
  "observation_health": "ready",
  "idempotency_health": "ready",
  "idempotency_retention_seconds": 604800,
  "idempotency_max_records": 2048,
  "idempotency_max_storage_bytes": 1048576,
  "client_request_id_max_length": 128
}
```

`general_groups` and `mentions` require at least one enabled, freshly verified
general destination. A supported backend with no enabled group returns false.
`mentions` describes parser support; every message separately reports whether
its mention data was available. Offline/unverifiable connections report false
for send/observation/group support. Health is `ready`, `degraded`, or
`unavailable`. A failed observation or message-tracking operation degrades
observation health for this runtime; diagnostics contain aggregate counters
without content, keys, routes, or provider error text. Missing idempotency
persistence disables new keyed sends while legacy sends remain independent.

Receipts, edits/deletions, media retrieval, manual-account observations, and
replay are unavailable in this release. An accepted send does not establish
delivery or read status. No media URLs, downloads, transcription, historical
imports, or long-term archive are added.

## Outbound observations

One actual send attempt through a configured notify destination,
`send_to_conversation`, or a configured-contact `send_poll` can publish one
`waha_whatsapp_event` with `type: message.outbound`:

```json
{
  "schema_version": 1,
  "event_id": "evt_outbound_example",
  "type": "message.outbound",
  "config_entry_id": "entry_example",
  "conversation_id": "direct_example",
  "conversation_type": "direct",
  "occurred_at": "2026-10-15T10:00:00+00:00",
  "origin": "automation",
  "client_request_id": null,
  "status": "accepted",
  "message": {
    "id": "msg_example",
    "kind": "text",
    "text": "The washing machine has finished.",
    "in_reply_to": null
  }
}
```

All IDs are opaque integration IDs. `conversation_type` is `direct` or
`group`; the route is the explicitly selected destination. Text includes the
actual recipient-scoped rendering, including a notify title. Poll messages
have `kind: poll`, `text` equal to the rendered question, and `options` equal
to all displayed option titles, including the synthetic No action choice.
They contain no executable action mapping. Quoted text observations preserve
the caller's opaque `message.in_reply_to` token.

`status` is `accepted`, `failed`, or `unknown`. Acceptance means the HTTP send
returned successfully; no message ID is required. Transport timeouts,
connection interruption, malformed/uncertain results, and server errors are
uncertain. Definite client rejections are failed. A redacted observation adds
`content_redacted: true` and omits text and poll options, retaining only safe
routing/correlation metadata and message kind. `message.id` can be null when
tracking failed after acceptance. Consumers must represent unknown outcomes
and observation gaps rather than invent a confirmed sent message.

`origin` is `automation`, `application`, `human`, or `unknown`. Notify and
poll paths use `automation`; replies default to `unknown` and accept caller
metadata. It is attribution, never authorization or verified human identity.
`fromMe` callbacks remain rejected. This does not observe messages sent
manually using the linked account.

Observation/tracking failures cannot convert a successful send into a failure
or submit it again. There is no external observer callback or dependency on a
consumer being online. Bus publication remains best effort; it is not a
durable inbox. A replay of a keyed service request publishes no second
outbound observation because it makes no second send attempt.

## Correlated text sends

Legacy unkeyed request/response behavior is preserved. Quotes remain bounded
by the private channel registry: seven days, at most 2048 messages, and earlier
expiry under retention pressure. A quote must belong to the same still-eligible
conversation. Failure never redirects to a replacement contact or phone.

```yaml
action: waha_whatsapp.send_to_conversation
data:
  conversation_id: direct_example
  message: "The task is complete."
  reply_to_message_id: msg_example # Optional, same conversation
  client_request_id: request_example # Optional, opaque 1–128 characters
  origin: application # Optional, defaults to unknown
  content_redacted: false # Optional; true removes observed content
response_variable: channel_result
```

A keyed response always includes:

```json
{
  "conversation_id": "direct_example",
  "message_id": "msg_reply_example",
  "client_request_id": "request_example",
  "status": "accepted",
  "replayed": false
}
```

The identity is `(config_entry_id, conversation_id, client_request_id)`.
The canonical digest includes message, opaque quote token, origin, and
explicit action redaction flag. Reusing a key with different data fails with
`request_conflict` before network I/O. An identical concurrent call returns
saved `pending`; a subsequent identical call returns the settled result with
`replayed: true`. A replay does not require an old quote still to exist. A
removed/disabled route rejects new requests and is never silently substituted;
retained keyed requests still replay their saved result without sending.

Intent is saved before the send using private atomic persistence that propagates
actual read/write failures. Corrupt existing state fails closed instead of
being treated as a new empty ledger. Connection unload fences new keyed
admissions and drains admitted sends; a replacement runtime waits for the
previous storage owner before restoring state. Optional quote tracking also
uses an exclusive channel-storage owner across reloads. Slow tracking can
finish in supervised background work after its observation deadline; unloading
drains that work, and a quote becomes usable only after its write completes.
Restored `pending` becomes `unknown`, and
an interrupted or uncertain submission is never automatically resent. A
successful transport submission whose final outcome cannot be persisted also
returns `unknown`. Missing optional quote tracking alone still returns
`accepted`, with `message_id: null`. There is no exactly-once delivery claim.
Do not generate a new key or switch to an unkeyed send to retry an uncertain
submission. Reconcile it outside the integration first.

Accepted/failed records are retained for seven days, up to 2048 records and
1 MiB of serialized state. The implementation reserves result capacity before
I/O. It never evicts an unresolved pending/unknown record to admit work;
uncertain records can outlive seven days and eventually fill the store. This
release provides no automatic retry or reconciliation action. After capacity
or persistence failure, new keyed requests fail before submission; duplicate
requests still return a retained known result. Beyond the advertised retention
window, a settled key is no longer protected: consumers must reconcile rather
than resubmit it. Loss/removal of private integration storage also loses
continuity; restore from backup before recovering external work.

Failed/unknown responses add a safe `error_code`:

| Code | Meaning |
| --- | --- |
| `invalid_conversation` | Route removed, disabled, changed, or ineligible |
| `invalid_reply_target` | Unknown, expired, or cross-conversation quote |
| `request_conflict` | Same key with a different canonical payload |
| `channel_unavailable` | Backend authentication/session rejected |
| `persistence_unavailable` | Intent cannot be durably admitted, including full storage |
| `rate_limited` | Explicit HTTP 429 rejection |
| `transport_failure` | Definite transport/client rejection |
| `outcome_unknown` | Submission may have occurred; do not blindly resend |

`pending` and successful `accepted` have no failure code. Raw provider errors
and credentials are excluded from keyed responses. Legacy unkeyed exceptions
retain their existing Home Assistant behavior. The legacy response contains
only `conversation_id` and, when tracked, `message_id`.

## Explicit general groups

Use **Add WhatsApp group** on the integration to enter the exact group JID
(or its numeric local ID), choose a label, and explicitly enable it. This is
a dedicated group subentry and native notify entity. Configuration reads the
exact group and current account's roster and binds the destination to that
account. A different connected account, missing bot membership, ambiguous
identity, disabled/removed config, or unavailable verification rejects sends
and ingestion. The managed guest group cannot be reused as a general group.
General-group operations never create groups or change participants, security
settings, or WhatsApp roles. The raw-phone `send_message` action continues to
accept individual phone numbers only; use the native group notify entity or
opaque group `send_to_conversation` route.

Each general-group callback follows the existing signed, local-only,
session-checked webhook. Only enabled configured groups are accepted. The
one-hour stale/five-minute future checks and `fromMe` rejection remain.
Events can arrive out of order; occurrence times are preserved and consumers
own ordering. No delivery-status callbacks are ingested in this release.
The group destination is the top-level route; the sender is a separate
participant. For example:

```json
{
  "schema_version": 1,
  "event_id": "evt_group_example",
  "type": "message.received",
  "config_entry_id": "entry_example",
  "conversation_id": "general_group_example",
  "conversation_type": "group",
  "occurred_at": "2026-10-15T10:00:00+00:00",
  "group": {"purpose": "general", "contact_id": "group_subentry_example"},
  "sender": {"participant_id": "participant_example", "identity_status": "verified"},
  "message": {
    "id": "msg_group_example",
    "kind": "text",
    "text": "Is anyone home?",
    "in_reply_to": null,
    "mentions": {"available": false, "bot": null, "participant_ids": []}
  }
}
```

No display name establishes identity. A resolved identity is represented only
by a group/account-scoped opaque participant ID. An unresolved sender has
`participant_id: null` and `identity_status: unresolved`, with no private route,
Person, user, or permissions. Conflicting verified aliases are rejected.
This differs deliberately from managed guest events: there is no membership
or stay object. General group messages never create `command.requested`.

`message.mentions` is available only when the supported GOWS message supplies
explicit, well-formed mention context and every mentioned identity resolves
against the verified roster. Then `bot` is a boolean and `participant_ids`
contains opaque mentioned participants. Missing/ambiguous metadata means
`available: false`, `bot: null`, and an empty list, not proof that the bot was
not mentioned. Message text is not parsed to infer mentions. Reactions retain
the existing `reaction.added`/`reaction.removed` types and scoped target
correlation. General group quotes are usable only in that same group.

Backend references: [WAHA receive-message fields](https://waha.devlike.pro/docs/how-to/receive-messages/),
[WAHA groups API](https://waha.devlike.pro/docs/how-to/groups/), and the
[GOWS normalization source](https://github.com/devlikeapro/waha/blob/core/src/core/engines/gows/session.gows.core.ts).

## Consumer integration and live verification

Sanitized payloads are in [channel-v1.json](examples/channel-v1.json).
Consumers should discover capabilities, retain their own durable inbox/outbox,
and deduplicate source events by identity and event type. Claim execution by
`(config_entry_id, event_id)` so the message and its command view cannot launch
two jobs; join both views before classifying a registered command. Likewise,
a settled poll's channel and legacy mobile-app event describe one choice.
Use fresh verified execution authorization for sensitive actions; route IDs,
Person attribution, WhatsApp roles, mentions, and bus payloads confer none.

The bus has no replay/catch-up guarantee. Retain disconnect gaps explicitly;
do not relax stale-event checks or treat historical messages as new commands.
Consumers own archives, summaries, route-to-scope mapping, and work recovery.
Integration capability discovery does not activate a consumer.

Before release/activation, manually verify on a test connection:

1. Existing notify, direct reply/quote, command, actionable poll and managed
   guest join/leave/private-route flows with observations disabled.
2. Enable observations; verify actual title/text and poll option rendering,
   one event per send, two recipients isolated, redaction on sensitive test
   content, and successful delivery while the consumer is disconnected.
3. Repeat one keyed send concurrently and after HA restart. Confirm one
   submission, conflict rejection, saved replay, pending-to-unknown recovery,
   and no automatic resend on network interruption or storage failure.
4. Configure one explicit general group; verify account/roster checks,
   separate sender/group IDs, available and unavailable mentions, quotes,
   reactions, duplicate callbacks, stale/out-of-order callbacks, and no
   commands. Disable/remove it and confirm old routes fail.
5. Verify diagnostics and downloaded traces contain no raw routing secrets,
   provider errors, or explicitly redacted outbound content. Confirm existing
   guest provisioning/security behavior remains independent.
