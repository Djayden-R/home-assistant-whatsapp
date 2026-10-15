# WhatsApp as a Home Assistant communication channel

Status: v1.4 direct-channel architecture with managed guest groups released
in v1.5.0, pending live HA/WhatsApp verification. Its
normative current contracts are in the [user guide](GUEST_GROUPS.md),
[API reference](GUEST_GROUP_API.md), [automation guide](GUEST_GROUP_AUTOMATIONS.md),
and [live test checklist](GUEST_GROUP_HA_TEST.md). The group sections below
are earlier design material and may differ from the implemented contract.
Voice transcription, access provisioning, and command execution remain
separate future work. The v1.6.0 command registry recognizes
explicitly registered private-chat slash commands and emits
`command.requested` without executing an action; see the
[command guide](COMMANDS.md).

The additive 1.7.0 implementation is documented in the [communication API](CHANNEL_API.md),
including outbound observations, keyed sends, and explicit general groups.
Those current contracts supersede the earlier group proposals below.

## Why this exists

Today the integration sends individual notifications and actionable polls. The
next useful step is not a larger collection of notification features: it is a
bidirectional channel that an automation, script, or conversation agent can use.
Sebastian should be able to send a new WhatsApp message to the home's number,
have Home Assistant receive it with a clear identity and conversation address,
and get a response in the same chat. The channel transports information; it
does not decide whether a message means "open the door".

This also changes the earlier roadmap. Outbound media notifications are not a
priority. Inbound audio transcription, group conversations, and commands are
more valuable, but each needs its own safety and product decisions.

## The model

An **event** is something WAHA observed. A text message, a voice note, a
reaction, and a group-member change are different event types. A **message**
is one kind of event and has a content kind (`text`, `audio`, `image`, `file`,
or `unknown`). A **conversation** is the destination to which Home Assistant
can send a response. The **sender** is separate from the conversation: they
are usually the same person in a direct chat, but not in a group.

The integration has two public surfaces:

1. Existing `notify` entities remain the simple way to start an outbound
   conversation with a configured contact.
2. A versioned `waha_whatsapp_event` on the Home Assistant event bus carries
   inbound activity. A new `waha_whatsapp.send_to_conversation` action takes
   the event's opaque `conversation_id` and sends text back to that chat.

The event bus is the programmatic interface for other integrations. A Home
Assistant event entity could later provide a convenient UI/history surface,
but its latest-state model is not a substitute for a stream of messages.
Home Assistant documents both [integration events](https://developers.home-assistant.io/docs/integration_events/)
and the [conversation processing action](https://www.home-assistant.io/actions/conversation.process/).
An AI agent is a *consumer* of this channel, not the channel itself. A consumer
may call `conversation.process`, keep that agent's conversation ID separately,
and send the answer back using this integration. The channel's
`conversation_id` is a WhatsApp routing identifier, **not** the conversation
agent's memory ID.

The first useful workflow need not involve AI: a configured contact sends
`status` and an opt-in automation returns a short home-status summary. A later
bridge can send arbitrary text to an Assist or AI conversation agent and
return its answer. This proves the channel is useful for both deterministic
automations and an agent, without forcing either into the integration.

## Proposed event contract

One event name with an explicit `schema_version` and `type` lets consumers
subscribe once while allowing future event types. Illustrative direct-message
event (IDs below are opaque examples, not WAHA JIDs):

```yaml
event_type: waha_whatsapp_event
data:
  schema_version: 1
  event_id: evt_opaque_123
  type: message.received
  config_entry_id: entry_123
  conversation_id: direct_opaque_456
  conversation_type: direct
  occurred_at: "2026-09-24T12:34:56+00:00"
  sender:
    contact_id: contact_opaque_789
    notify_entity_id: notify.waha_seba
    person_entity_id: person.seba # Omitted if no Person is associated
  message:
    id: msg_opaque_abc
    in_reply_to: null
    kind: text
    text: "Are we home?"
```

`event_id` is for consumer deduplication. `message.id` identifies a WhatsApp
message within this integration. `conversation_id` is a stable routing handle
for the configured direct contact, including across Home Assistant restarts
and contact renames. It deliberately changes if that contact's phone number
changes, so an old event cannot route a delayed private reply to a new number.
The integration should not expose phone numbers, raw WhatsApp JIDs, WAHA
message IDs, webhook credentials, or the complete WAHA payload on the event
bus. The current direct `send_message` action's raw WAHA response is a legacy
surface; this proposal does not silently change it.

Other proposed v1.4 events use the same envelope:

```yaml
type: reaction.added # or reaction.removed
reaction:
  emoji: "👍" # Empty/absent for removal
  target_message_id: msg_opaque_abc
  target_known: true
```

`target_known` means the integration observed or sent the target message; it
does not imply that a reaction is an instruction. A reaction to an untracked
message can still be reported with an opaque target and `target_known: false`
when WAHA supplies enough information to verify that it belongs to the same
configured direct conversation. Consumers should require a known target before
assigning action semantics. An empty reaction from WAHA represents removal,
not a negative vote. Since v1.4.1, a poll choice that survives its correction
window publishes `type: poll.selection_settled` with an opaque poll message ID,
the selected option, and nullable action ID. The existing
`mobile_app_notification_action` event stays separate and unchanged; consumers
must not execute an action from both event types. The synthetic "No action"
choice is observable in the channel event without firing a legacy action.

Reactions are most plausible as lightweight acknowledgements of a *specific*
notification (for example, "I saw this"). They are a poor substitute for
the existing poll when the user must choose among named actions, and v1.4
should not invent universal meanings for 👍, ❤️, or other emoji.

For a non-text inbound message, v1.4 would emit a typed *placeholder* such as
`message.kind: audio` with safe metadata (for example MIME type), but no media
URL, binary content, or download. A caption, if present, must be a separate
field rather than being mistaken for a text command. This makes the event
model useful before a media processor exists, without committing us to
outbound media notifications.

## Boundaries and safety

- In v1.4, accept new direct-chat messages and reactions from **configured
  contacts only**, including messages that are not replies to something the
  integration sent. Unknown senders and all groups are ignored by default.
- Keep the current signed, local-only WAHA webhook and configured session
  checks. Subscribe only to the needed events; avoid `message.any`, which also
  includes the integration's own outgoing messages. Reject `fromMe` events and
  prevent a reply loop. WAHA documents these [event distinctions](https://waha.devlike.pro/docs/how-to/events/).
- Resolve GOWS phone-number/LID variants against the configured contact, as
  already needed for polls. If identity cannot be resolved unambiguously,
  do not emit an actionable event. Never treat a display name as identity.
- Person association helps consumers identify a contact. It does not modify a
  Person, authorize a command, or prove that the Person is physically holding
  the phone. Any Home Assistant `context.user_id` attribution must be
  documented as attribution, **not** a permission grant.
- Raw message content is necessarily available to event listeners and may
  appear in automation traces. Do not log it or include it in diagnostics.
  Log only redacted rejection reasons and counts.
- WAHA webhooks can retry. Emit a stable event ID and suppress duplicate
  deliveries within a bounded window, but do not promise exactly-once delivery
  or a durable inbox. Consumers that perform sensitive actions must remain
  idempotent.
- Discard a message or reaction whose WAHA timestamp is more than one hour
  old or over five minutes in the future. This protects opt-in consumers from
  acting on stale history after a reconnect; it deliberately does not provide
  catch-up delivery while Home Assistant is offline.

## What becomes possible later

| Follow-up | Fits the same model | Extra decision needed |
| --- | --- | --- |
| Incoming voice transcription (working label v1.4.5) | `message.received` with `kind: audio`, then a derived `transcription.completed` event linked by `source_message_id` | Choose the transcription engine, language detection, retention, file-size limits, and whether audio ever leaves the mini PC. |
| Group destinations and guest chat | `conversation_type: group`; `sender` remains the individual participant. Group join/leave are `group.participant.joined` and `group.participant.left` events, not messages. | Explicit group allowlist and identity mapping; what happens when a group member is unknown. |
| Command execution and AI assistant | The v1.6.0 registry only recognizes explicitly allowed private text commands and publishes `command.requested`. A separate consumer may interpret requests and reply via the channel. | Verified execution authorization, especially for doors, locks, PINs, and guest access; public HA bus events alone are not sufficient proof. |
| Acknowledgements or message edits | Additional event `type` values under the same versioned envelope. | Whether a real use case justifies them and how to handle order/duplication. |

Group membership can become a *signal* that a guest arrived or left, but it
must not itself create a door PIN or authorize entry. Those are separate,
explicitly approved workflows with expiry and revocation. This is consistent
with the guest-access direction in [the notification-system notes](ha_notification_system.md).
[WAHA's group participant events](https://waha.devlike.pro/docs/how-to/groups/)
make such a workflow technically possible when group support is intentionally
added.

## Product choices still worth discussing

1. Should v1.4 always emit reactions, or make them an opt-in event category?
   They are inexpensive to expose but need a real consumer to be useful.
2. Should the first assistant recipe use Home Assistant's existing
   `conversation.process` or a separate AI integration? The transport can
   support either; automatic agent routing is not part of v1.4.
3. How long should a WhatsApp quoted-reply target remain available? Sending a
   normal message to the same `conversation_id` must continue to work even
   after that target expires.

These choices do not block the basic channel: configured sender → event →
consumer → `send_to_conversation` → same WhatsApp chat.
