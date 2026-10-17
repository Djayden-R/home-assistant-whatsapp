# Home Assistant WhatsApp

Self-hosted WhatsApp notifications for Home Assistant, powered by
[WAHA](https://waha.devlike.pro/).

This repository contains the two pieces needed for a native experience:

- **WAHA for Home Assistant**, an HAOS app that runs and preserves the linked
  WhatsApp session and provides a sidebar control panel.
- **WAHA WhatsApp**, a HACS custom integration that creates native Home
  Assistant notify entities and sends through the app's private API.

There are no Meta templates or per-message provider fees. WAHA uses an
unofficial WhatsApp protocol, so use a dedicated, replaceable WhatsApp number
and understand that WhatsApp can restrict it.

## Current functionality

- Free-form outbound WhatsApp notifications.
- Single-selection WhatsApp polls that reuse Home Assistant Companion App
  action IDs and the `mobile_app_notification_action` event.
- A native `notify` entity for every configured individual contact.
- One opt-in managed guest group per WAHA connection, with membership events,
  group notifications, and private per-stay member routes in integration
  version 1.5.0 (GOWS 2026.8.2 or newer required).
- Optional association of a contact with an existing Home Assistant Person.
- Automatic, private discovery of the HAOS app by the HACS integration.
- Manual connection support for WAHA running elsewhere on the network.
- A direct action for sending to an arbitrary phone number.
- Structured events for inbound direct messages and reactions from configured
  contacts, plus a text action that replies to the same conversation.
- Administrator-managed private-chat slash commands for selected contacts or
  current guests, emitting `command.requested` events without executing HA actions
  (integration v1.6.0).
- QR linking, session lifecycle controls, diagnostics, and persistent backups.

The managed guest group is included in integration version 1.5.0, but it has
not yet passed the manual live HA/WhatsApp test. Check the compatible
GOWS requirement and readiness steps before enabling it. Arbitrary group
discovery, voice transcription, access provisioning, and command execution
remain out of scope. Inbound messages never run Home Assistant actions by
themselves; an automation or conversation agent must explicitly consume them.

Managed guest-group references:

- [User guide](docs/GUEST_GROUPS.md)
- [Event and action API](docs/GUEST_GROUP_API.md)
- [Automation guidance](docs/GUEST_GROUP_AUTOMATIONS.md)
- [Manual live test checklist](docs/GUEST_GROUP_HA_TEST.md)
- [Design and implementation plan](docs/plans/MANAGED_GUEST_GROUP.md)

See the [command guide](docs/COMMANDS.md) for registration, event format,
security boundaries, and a first live test. A command declaration does not
run Home Assistant actions or grant access from WhatsApp group membership.

## Communication API

Integration 1.7.0 adds optional outbound observations, durable correlated text
sends, capability discovery, and explicitly configured general group notify
destinations. See the [communication API](docs/CHANNEL_API.md) for setup,
schemas, privacy controls, compatibility, and live verification. New content
observation and general groups are disabled by default. The integration remains
a communication layer: external consumers decide what messages mean and what
work is authorized.

Integration 1.8.0 adds opt-in authenticated media transport: incoming voice,
audio, image and PDF descriptors, on-demand downloads, private upload staging,
and durable correlated image/document/audio sends. See the [media API and
consumer fixtures](docs/MEDIA_API.md) and [live checklist](docs/MEDIA_HA_TEST.md).
Media is off by default and requires an active HA administrator credential.
App 0.2.4 prepares the matching private media URL origin and keeps the existing
digest-pinned GOWS 2026.9.1 engine. Keep `download_media: false` for per-message
retrieval. Transcription and
speech generation belong to external consumers.

## Requirements

- Home Assistant 2026.7 or newer.
- Home Assistant OS on `amd64` for the included app, or an independently
  managed WAHA server.
- HACS for the native Home Assistant integration.
- A dedicated WhatsApp account linked to WAHA as a companion device.

## Install the HAOS app

1. Go to **Settings → Apps → App store → Repositories**.
2. Add:

   ```text
   https://github.com/sebastian-greco/home-assistant-whatsapp
   ```

3. Refresh the app store and install **WAHA for Home Assistant**.
4. Configure a random API key of at least 16 characters; 32 or more is
   recommended.
5. Start the app and enable **Show in sidebar**.
6. Open **WAHA**, show the QR code, and scan it from WhatsApp under
   **Linked devices**.
7. Wait for the session status to become `WORKING`.

Keep port `3000` disabled. The HACS integration communicates with WAHA over
Home Assistant's private app network and receives the connection details
through Supervisor discovery.

## Install the HACS integration

1. Open HACS and select **Custom repositories**.
2. Add the same repository URL with type **Integration**.
3. Download **WAHA WhatsApp** and restart Home Assistant.
4. Restart the **WAHA for Home Assistant** app once so it republishes
   discovery if necessary.
5. Open **Settings → Devices & services** and select the discovered
   **WAHA WhatsApp** card.

If discovery is unavailable, select **Add integration → WAHA WhatsApp** and
enter a reachable WAHA URL, API key, and session manually.

## Add contacts

Open the configured WAHA WhatsApp integration and select
**Add WhatsApp recipient**. For each recipient:

- Enter a contact name, or select an existing `person.*` entity to use its
  current name.
- Enter their international WhatsApp phone number including country code.

The optional Person association is identity metadata only. The integration
does not modify the Person entity and does not store notification preferences
or memberships on it. A contact does not need a Home Assistant user or Person.

The integration creates entities similar to:

- `notify.waha_seba`
- `notify.waha_lucila`

Adding, updating, or removing a recipient automatically refreshes the
individual contact entities. Home Assistant may retain an existing entity ID
or add a suffix, so confirm the actual ID in the entity picker.

When a contact is associated with a Person, its notify entity exposes the
non-sensitive `person_entity_id` state attribute:

```jinja
{{ state_attr('notify.waha_seba', 'person_entity_id') }}
```

This returns an entity ID such as `person.seba`. Contacts without a Person do
not have the attribute. Phone numbers are never exposed as entity state
attributes.

## Send a notification

Use the modern Home Assistant notify action:

```yaml
actions:
  - action: notify.send_message
    target:
      entity_id: notify.waha_seba
    data:
      title: Garage warning
      message: The garage door has been open for 10 minutes.
```

The WhatsApp message is rendered as a bold title followed by the details. To
notify multiple people today, explicitly target each configured individual
contact from your automation or notification router.

Existing notification routers can keep their `title` and `message` contract:

```yaml
sequence:
  - variables:
      whatsapp_targets:
        sebastian: notify.waha_seba
        lucila: notify.waha_lucila
      whatsapp_target: "{{ whatsapp_targets.get(person) }}"

  - action: notify.send_message
    continue_on_error: true
    target:
      entity_id: "{{ whatsapp_target }}"
    data:
      title: "{{ final_title }}"
      message: "{{ message }}"
```

Routers can also discover the individual WAHA notify entity dynamically from a
Person entity ID instead of maintaining a contact-name map:

```yaml
sequence:
  - variables:
      whatsapp_target: >-
        {{ states.notify
           | selectattr('attributes.person_entity_id', 'eq', person_entity_id)
           | map(attribute='entity_id')
           | first
           | default(none) }}

  - if:
      - condition: template
        value_template: "{{ whatsapp_target is string }}"
    then:
      - action: notify.send_message
        target:
          entity_id: "{{ whatsapp_target }}"
        data:
          title: "{{ final_title }}"
          message: "{{ message }}"
```

The router should handle the no-match case because contacts are allowed to
exist without a Person association.

For a one-off number that is not configured as a Person, use the direct
integration action:

```yaml
actions:
  - action: waha_whatsapp.send_message
    data:
      config_entry_id: YOUR_CONFIG_ENTRY_ID
      to: "+39 333 123 4567"
      title: Washing machine
      message: The cycle has finished.
      link_preview: true
```

The automation editor provides a config-entry picker, so the ID does not need
to be typed when building the action in the UI.

## Send an actionable notification

Use `waha_whatsapp.send_poll` when a notification has Companion App-style
`actions`. It targets one configured WAHA notify entity and sends a WhatsApp
single-selection poll:

```yaml
actions:
  - action: waha_whatsapp.send_poll
    data:
      entity_id: notify.waha_seba
      title: Sleep mode
      message: Sleep mode starts in two minutes. Do you want to cancel it?
      actions:
        - action: CANCEL_SLEEP_MODE
          title: Cancel
      no_action_title: Keep scheduled
      settle_seconds: 5
```

WhatsApp requires at least two poll options. When there is only one real
action, the integration adds `no_action_title` as a second, non-triggering
choice. Its default label is `No action`. With two or more real actions, each
action becomes one option and no synthetic choice is added.

After the latest choice remains unchanged for `settle_seconds` (five seconds
by default), a real action choice fires the standard Home Assistant event:

```yaml
event_type: mobile_app_notification_action
data:
  action: CANCEL_SLEEP_MODE
```

Since v1.4.1, every settled choice on a newly sent poll also publishes a
`waha_whatsapp_event` with `type: poll.selection_settled`. This includes the
synthetic **No action** choice, for which `poll.action_id` is null. It is an
additional channel event, not a second execution of the action:

```yaml
event_type: waha_whatsapp_event
data:
  schema_version: 1
  type: poll.selection_settled
  conversation_id: direct_opaque_456
  poll:
    message_id: msg_opaque_abc
    selected_option: Cancel
    action_id: CANCEL_SLEEP_MODE
```

The channel event also has `event_id`, `config_entry_id`, `occurred_at`,
`conversation_type: direct`, and the same safe `sender` metadata used for
messages and reactions. Neither event is emitted for a deselection left with
no choice. Polls sent before updating to 1.4.1 have no saved conversation
association, so they continue to produce only the legacy action event.
Automations should not execute the same action from *both* event types.

The `mobile_app_notification_action` event is the same contract used by Home
Assistant Companion App actionable notifications. An existing handler like
this works for either the
mobile notification button or the WhatsApp poll without another trigger:

```yaml
triggers:
  - trigger: event
    event_type: mobile_app_notification_action
    event_data:
      action: CANCEL_SLEEP_MODE
```

The integration does not call services, enforce workflow expiration, or send
an automatic confirmation. The receiving automation remains responsible for
conditions, timers, the actual action, and any follow-up notification. This is
important for old polls: add a current-state guard in the action handler when
an action should no longer be valid after the surrounding workflow changes.

When the configured WhatsApp contact has a Person association and that Person
is linked to an active Home Assistant user, the settled event has that user's
ID in `trigger.event.context.user_id`. This mirrors Companion App attribution
and allows one shared action handler to identify who responded. The integration
stores the optional `person_entity_id` and an opaque conversation handle with
the pending poll; it resolves the Person's current user when the vote settles
and never includes the user ID in event data. Contacts without a linked active
user still fire the same action with a null `context.user_id`. WhatsApp
responses use Home Assistant's local event origin.

The five-second correction window handles a quick misclick: a newer vote
replaces the earlier choice and restarts the timer. A poll is consumed after
its first settled choice, including the non-triggering option, so later edits
cannot fire a second event.

The integration automatically registers a private Home Assistant webhook and
adds it to the configured WAHA session for `poll.vote`, `poll.vote.failed`,
`message`, and `message.reaction`. HAOS app installations use only the internal
app network;
there is no port forwarding, cloud callback, or manual webhook setup. Every
request must have WAHA's SHA-512 HMAC signature, and the integration also
correlates the poll message ID, session, and configured recipient before
accepting a vote. Existing WAHA webhooks are preserved.

If WAHA reports `poll.vote.failed`, or an authenticated vote fails validation,
the integration deliberately fires no action and sends no automatic message.
Sanitized warnings and per-reason counters appear in Home Assistant diagnostics
without message IDs, contact identifiers, phone numbers, or poll contents.

GOWS can report the poll and voter with WhatsApp's alternate `@lid` identity
even when the poll was sent to a phone-number `@c.us` chat ID. WAHA's serialized
message-ID chat envelope can change with that identity, so the integration
correlates its engine-stable WhatsApp message token and then resolves every LID
through WAHA's session mapping API. The mapped voter and poll destination must
both equal the configured phone-number chat. The private HMAC callback, session,
outgoing direction, option, and timestamp checks also remain required; another
phone-number JID, an unmapped LID, or a group poll is never accepted.

### Use the same router data as Companion App actions

A central notification router can continue accepting `data.actions` from its
callers. For its WhatsApp branch, select the poll action when the list is not
empty and the ordinary notify action otherwise:

```yaml
- variables:
    notification_actions: "{{ data.actions | default([], true) }}"

- if:
    - condition: template
      value_template: "{{ notification_actions | count > 0 }}"
  then:
    - action: waha_whatsapp.send_poll
      continue_on_error: true
      data:
        entity_id: "{{ whatsapp_target }}"
        title: "{{ final_title }}"
        message: "{{ message }}"
        actions: "{{ notification_actions }}"
  else:
    - action: notify.send_message
      continue_on_error: true
      target:
        entity_id: "{{ whatsapp_target }}"
      data:
        title: "{{ final_title }}"
        message: "{{ message }}"
```

Harmless mobile-only action fields can remain in the dictionaries; the
WhatsApp bridge uses only `action` and `title`. Text-input/`REPLY` and `URI`
actions are rejected because a poll cannot preserve those semantics. The
router still decides whether to send to the Companion App, WhatsApp, or both.

## Receive a WhatsApp message

The integration publishes `waha_whatsapp_event` for direct messages,
reactions, and settled poll selections from configured contacts. A new
message does not need to reply to a notification. Unknown senders are not
published. Group chats are published only for the opt-in managed guest group;
other groups are ignored. Receiving a message never executes a
Home Assistant action by itself. Message and reaction events more than one
hour old, or more than five minutes in the future, are discarded to prevent
stale history from acting like a new request after a reconnect. If WAHA runs
on another machine, keep its clock synchronized with Home Assistant's.

An inbound text event has this shape (IDs are opaque, not phone numbers):

```yaml
event_type: waha_whatsapp_event
data:
  schema_version: 1
  event_id: evt_opaque_123
  type: message.received
  conversation_id: direct_opaque_456
  conversation_type: direct
  sender:
    notify_entity_id: notify.waha_seba
    person_entity_id: person.seba # Omitted if no Person is associated
  message:
    id: msg_opaque_abc
    kind: text
    text: ping
```

The event also includes `config_entry_id`, `occurred_at`, and an opaque
`sender.contact_id`; `message.in_reply_to` is present when WAHA supplies a
quoted-message ID. For audio, image, or file messages, `kind` identifies the
type; additive attachment descriptors distinguish verified voice notes from
ordinary audio. Media is retrieved only through the authenticated
[media API](docs/MEDIA_API.md), and no backend URL is exposed. Reactions use
`type: reaction.added` or `reaction.removed`, with a `reaction` object that
contains an emoji and opaque target message ID. Reactions do not trigger
actions automatically. Listen for the event under **Developer tools → Events**
to inspect your installation's exact payload.

This opt-in automation is a safe first test. Replace the entity ID with your
configured contact's notify entity:

```yaml
alias: WAHA ping test
triggers:
  - trigger: event
    event_type: waha_whatsapp_event
    event_data:
      type: message.received
conditions:
  - condition: template
    value_template: >-
      {{ trigger.event.data.sender.notify_entity_id == 'notify.waha_seba'
         and trigger.event.data.message.kind == 'text'
         and trigger.event.data.message.text | trim | lower == 'ping' }}
actions:
  - action: waha_whatsapp.send_to_conversation
    data:
      conversation_id: "{{ trigger.event.data.conversation_id }}"
      message: pong
```

`send_to_conversation` can be used by any automation or integration that has a
current `conversation_id`. It sends text to that configured contact without
placing their phone number in YAML. Pass `reply_to_message_id` from an inbound
event if you want WhatsApp to quote that particular message; omit it for a
normal message. Quoted-message IDs are retained for a bounded period, while
plain sending remains available as long as the contact is configured.
Changing a contact's phone number creates a new conversation ID; old IDs are
rejected rather than silently sending to the replacement number.
The event bus is best effort, not a durable inbox: WAHA retries are
deduplicated within a bounded window, but delivery and exactly-once execution
are not guaranteed. Consumers performing important actions should use the
event ID to make their work idempotent. Message text can appear in Home
Assistant automation traces, so treat those traces as private.
If the channel's private state cannot be restored at startup, existing
notifications and polls still load while the channel uses temporary
in-memory tracking; diagnostics report when this fallback is active. Home
Assistant can log some storage write failures without notifying the
integration, so this flag is not a guarantee of durable writes.

An AI agent can consume the same event with an explicitly enabled automation:
pass `message.text` to Home Assistant's `conversation.process`, then send
`agent_response.response.speech.plain.speech` to the event's
`conversation_id`. Allowlist the contacts and agent you intend to use before
doing this: a conversation agent may have permission to control devices.
The agent's own memory `conversation_id` is separate from the WhatsApp routing
ID and must be stored separately if you want multi-turn context. See the
[channel design](docs/WHATSAPP_CHANNEL_DESIGN.md) for the event model and
future voice/group direction. After updating, follow the
[HAOS verification checklist](docs/V1_4_0_HA_TEST.md) before connecting an
agent or changing any safety-sensitive automations.

A generic opt-in broadcast to every configured contact is a possible separate
feature. It is not a household group and is not included in the current
release.

For a staged transition from Companion App notifications, including dual
delivery, actionable polls, Alarmo, priority, and mobile-only payloads, follow
the [notification migration guide](docs/NOTIFICATION_MIGRATION.md).

## Migrating from the Kapso integration

Version `1.0.0` replaces the old `kapso_whatsapp` domain with
`waha_whatsapp`; it is deliberately a clean provider migration because Kapso
credentials and templates cannot be converted into a WAHA linked session.

Before installing this version:

1. Remove the **Kapso WhatsApp** integration from Devices & services.
2. Remove its old HACS download and restart Home Assistant.
3. Install the WAHA app and the new HACS integration using the instructions
   above.
4. Re-add recipients by selecting their existing Person entities.
5. Update automation targets if Home Assistant did not preserve the same
   desired notify entity IDs.

The complete Kapso source and documentation remain permanently available on
the [`legacy-kapso`](https://github.com/sebastian-greco/home-assistant-whatsapp/tree/legacy-kapso)
branch. Existing `v0.1.0` and `v0.2.0` release tags are unchanged.

## Security

- Never expose WAHA port 3000 to the internet.
- Keep the sidebar restricted to Home Assistant administrators.
- Keep automatic WAHA media downloads disabled when using on-demand retrieval.
- Enable WhatsApp two-step verification and configure a recovery email.
- Keep the dedicated SIM/eSIM recoverable in a normal phone.
- API keys and phone numbers are redacted from downloaded diagnostics.

## Versions and development

The two deliverables are independently versioned:

- HACS integration: `custom_components/waha_whatsapp/manifest.json`, released
  as `vMAJOR.MINOR.PATCH`.
- HAOS app: `waha/config.yaml`, released as `waha-vMAJOR.MINOR.PATCH` and
  published as `ghcr.io/sebastian-greco/ha-waha:VERSION`.

Local validation:

```bash
uv run ruff check .
uv run python scripts/check_version.py
uv run python -m compileall -q custom_components tests
uv run pytest
node --check waha/run.mjs
node --check waha/control.mjs
node --check waha/discovery.mjs
node --test waha/tests/*.test.mjs
```

See [CHANGELOG.md](CHANGELOG.md), [RELEASING.md](RELEASING.md), and
[`waha/DOCS.md`](waha/DOCS.md) for more details.
