# Read receipts and typing activity API

This document describes generic schema-v1 read-receipt and transient typing
transport for configured WhatsApp conversations. The consumer decides when a
message is considered read and when composing starts or stops. The integration
validates and transports those explicit requests; it does not infer activity
from message delivery, events, or other application state.

The API adds no conversation events, outbound message, notification, send-ledger
entry, or activity setting. Existing message, poll, media, command, guest, and
group interfaces keep their existing contracts.

## Requirements and discovery

Activity transport requires a verified, connected WAHA **GOWS 2026.9.1 or
newer** session. Other engines and older GOWS versions do not advertise or
receive these operations. The packaged HAOS app pins GOWS 2026.9.1. Unsupported
activity does not disable ordinary message transport.

Call `waha_whatsapp.get_channel_capabilities` for the loaded connection. The
response includes an `activity_contract` describing the active backend and
session:

```json
{
  "activity_contract": {
    "schema_version": 1,
    "mark_read": true,
    "typing": true,
    "read_scope": "message"
  }
}
```

If the backend/session cannot support the operation, the corresponding flag
is `false`. `read_scope` is always `message`; a consumer must never substitute
chat-wide reading when exact-message reading is unavailable. These flags are
separate from delivery-receipt reporting.

## Access and identifiers

Both activity services require an active Home Assistant administrator. The
caller must provide the loaded integration's `config_entry_id` and an existing
opaque `conversation_id`. Activity calls recheck authorization and the current
configured route at the transport boundary. Raw WhatsApp addresses and backend
IDs are not accepted as substitutes for these integration-issued handles.
Eligible routes include configured direct conversations, explicitly
configured groups, and current guest routes. A group read also requires the
incoming message's verified participant binding and a current roster or
membership check. Changed routes are refused rather than reassigned.

The read service accepts one opaque `message_id` from an observed incoming
message on that conversation. Conversation and message identifiers must be
strings from 1 through 256 characters. Unknown, expired, outgoing, malformed,
cross-conversation, or cross-entry message references are refused before any
network effect. A message must still belong to the current configured route.

Incoming message references follow the existing private channel retention:
seven days and at most 2048 tracked messages, with earlier expiry under
retention pressure. Existing quote data remains usable for its established
reply behavior, but legacy references, nonstandard provider IDs without verified
incoming provenance, and outgoing messages are not eligible for read marking.
A refused read never falls back to marking the whole conversation read.

## Mark one message read

`waha_whatsapp.mark_conversation_read` supports response variables:

| Field | Required | Validation |
| --- | --- | --- |
| `config_entry_id` | Yes | String, 1–256 characters; loaded integration entry |
| `conversation_id` | Yes | String, 1–256 characters; current opaque route |
| `message_id` | Yes | String, 1–256 characters; eligible incoming message on that route |

Example action:

```yaml
action: waha_whatsapp.mark_conversation_read
data:
  config_entry_id: entry_example
  conversation_id: direct_example
  message_id: msg_incoming_example
response_variable: read_result
```

Every response echoes `schema_version: 1`, `conversation_id`, and
`message_id`. `status` is `accepted`, `failed`, or `unknown`. For example:

```json
{
  "schema_version": 1,
  "conversation_id": "direct_example",
  "message_id": "msg_incoming_example",
  "status": "accepted"
}
```

An accepted result means WAHA accepted the exact-message operation. It does not
prove that a particular phone displayed blue ticks; WhatsApp privacy settings
still apply. Repeated exact-message reads are harmless.

## Set or clear typing

`waha_whatsapp.set_conversation_typing` supports response variables:

| Field | Required | Validation |
| --- | --- | --- |
| `config_entry_id` | Yes | String, 1–256 characters; loaded integration entry |
| `conversation_id` | Yes | String, 1–256 characters; current opaque route |
| `activity_id` | Yes | ASCII string, 1–128 characters, matching `[A-Za-z0-9][A-Za-z0-9._:-]*` |
| `state` | Yes | `typing` or `paused` |
| `ttl_seconds` | For `typing`, optional | Integer 3–15, default 10; booleans are rejected. Omit for `paused`. |

Example start/refresh action:

```yaml
action: waha_whatsapp.set_conversation_typing
data:
  config_entry_id: entry_example
  conversation_id: direct_example
  activity_id: activity_example_01
  state: typing
  ttl_seconds: 10
response_variable: typing_result
```

Use the same `activity_id` to refresh the current activity and renew its
lease. Use `state: paused` with that ID to stop it:

```yaml
action: waha_whatsapp.set_conversation_typing
data:
  config_entry_id: entry_example
  conversation_id: direct_example
  activity_id: activity_example_01
  state: paused
response_variable: typing_result
```

Each response echoes `schema_version: 1`, `conversation_id`, and
`activity_id`. It includes `status` (`accepted`, `failed`, or `unknown`) and
`applied`: `true` when an accepted update was applied, `false` for a failed or
no-op update, and `null` when the backend outcome is uncertain. A duplicate or
stale pause is harmless and returns `accepted` with `applied: false`.

One ID represents one activity lifetime. A new ID may replace the active
activity. A late refresh, pause, or expiry from an older ID cannot revive or
clear the replacement. Operations are serialized per session. Retired IDs are
kept for the runtime so stale cleanup stays safe; retained activity state is
bounded to 256 conversation lifetimes and 2048 activity IDs per loaded entry.
These records are not evicted to make room. A new conversation lifetime or
unique activity ID beyond its limit returns `rate_limited`; refreshes of the
current activity remain available. The bounds reset when the integration
runtime is reloaded.

Typing leases expire on native Home Assistant timers if refreshes stop. A
pause or lease-expiry cleanup sends `paused` to the original destination only
while the original session, account, and route binding still verify. On
integration unload or shutdown, it attempts the same cleanup. When the last
owned activity ends, the integration sends WAHA's session-wide `offline`
cleanup only while the original session and account still verify. If the
backend or required identity check is unavailable, cleanup is best effort.
Typing is ephemeral and is never restored or replayed after reconnect or
restart. A lost response does not extend the lease indefinitely; its timer
still expires the local activity. These operations do not change the WAHA
engine or its global presence defaults.

## Responses and errors

Failed and unknown results may include a bounded `error_code`. Safe codes are:

| Code | Meaning |
| --- | --- |
| `invalid_request` | A field or state does not meet the service schema |
| `not_authorized` | Caller is not an active Home Assistant administrator |
| `invalid_conversation` | Route is unknown, removed, changed, or no longer eligible |
| `invalid_message` | Message is unknown, expired, outgoing, or belongs elsewhere |
| `unsupported` | Active backend does not support this operation |
| `channel_unavailable` | Connection or session is not available |
| `rate_limited` | A safe runtime bound or backend limit rejected the request |
| `transport_failure` | Backend definitively rejected or failed the operation |
| `outcome_unknown` | The request may have reached WAHA, but no final result was received |

Preflight refusals happen before network effects. A lost backend response is
reported as `unknown`, not as a confirmed failure, and is never retried
automatically. Do not blindly repeat an uncertain request. Responses and public
logs omit private backend and message data, including raw provider errors.

## Consumer and live verification

The synthetic consumer fixtures are in
[activity-v1.json](examples/activity-v1.json). Consumers should discover
capabilities before use and make their own read and typing decisions. A
capability flag of `false` means to skip that operation; ordinary messaging
remains available.

Before enabling a consumer, verify on a test connection:

1. Capability values for a connected supported session and an unsupported
   engine/version.
2. Exact incoming-message reads, including duplicate reads, and preflight
   refusal for outgoing, expired, unknown, cross-route, and removed-route IDs.
3. Typing refresh, explicit pause, automatic lease expiry, unload/shutdown
   cleanup, and no replay after reconnect or restart.
4. A replacement activity racing with delayed refresh, pause, and expiry from
   the retired activity; verify the replacement stays active.
5. Backend timeouts and route changes, checking unknown outcomes, safe errors,
   bounded state, and absence of activity replay.
6. WhatsApp read privacy behavior and phone notification cleanup after typing
   stops. A successful API response does not establish what a phone displayed.

Source and synthetic checks do not establish live Home Assistant/WhatsApp
behavior. The services must be tested against a non-production connection
before activation.

Backend references: [WAHA send-message APIs], [WAHA presence API], and the
[GOWS 2026.9.1 session implementation].

[WAHA send-message APIs]: https://waha.devlike.pro/docs/how-to/send-messages/
[WAHA presence API]: https://waha.devlike.pro/docs/how-to/presence/
[GOWS 2026.9.1 session implementation]: https://github.com/devlikeapro/waha/blob/2026.9.1/src/core/engines/gows/session.gows.core.ts
