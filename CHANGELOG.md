# Changelog

All notable changes are documented here. This project follows
[Semantic Versioning](https://semver.org/).

## [Unreleased]

## [1.10.0] - 2026-10-20

### Added

- Add generic `mark_conversation_read` and `set_conversation_typing` services
  with schema-v1 response contracts, explicit capability reporting, exact
  incoming-message eligibility, and bounded transient typing leases.
- Document synthetic consumer fixtures, safe failure outcomes, GOWS version
  requirements, and manual verification steps. Consumers own all read and
  typing decisions.

### Compatibility

- Keep existing messaging, polls, media, commands, guest and group behavior.
  Activity transport requires GOWS 2026.9.1 or newer; unsupported activity
  leaves ordinary message transport available. Live phone behavior remains to
  be verified before activation.
- App 0.2.4 and its pinned GOWS 2026.9.1 engine are unchanged; no app release
  is needed. Publication does not install or activate the integration.

## [1.9.0] - 2026-10-19

### Added

- Add `send_poll_to_conversation` for immutable, keyed application-owned polls
  to existing direct contacts, including accepted/failed/unknown outcomes,
  conflict detection and duplicate protection through the existing send ledger.
- Add authenticated application `poll.selection_settled` events with the
  original vote time, client request ID and ordered selection sequence.
  Application choices do not fire `mobile_app_notification_action`; their
  visible labels are omitted from the event for privacy.
- Add `get_poll_selections` for reconnect recovery, bounded to seven days and
  the latest 2048 application decisions per connection. Preserve strict atomic
  acknowledged persistence before publication, with failed-write recovery.
- Advertise `application_polls` and `poll_selection_replay` capabilities and
  document the generic send, event and recovery contracts.

### Compatibility

- Existing notify, text, media and automation-owned poll contracts remain.
  Reminder scheduling, task completion, health records and agent decisions
  remain entirely in consumers; the integration only transports choices.
- The HAOS app stays at 0.2.4 with its existing engine and download defaults.
- Source review and synthetic tests do not establish a live phone test.
  Update/restart Home Assistant separately before using the new actions.

## [1.8.1] - 2026-10-18

### Fixed

- Accept an authenticated, exactly bound inbound GOWS PTT Ogg/Opus stream
  ending between complete pages and packets without an EOS flag. Preserve the
  original bytes, size and SHA-256; retain framing, CRC, sequence, header,
  stream, authorization and resource checks.
- Keep the default Ogg policy strict for ordinary audio, uploads and outbound
  native voice. Clients cannot select the inbound compatibility policy.
- Add CRC-correct synthetic EOF regressions and scoped retrieval/download
  verification. This is transport compatibility, without audio repair or STT.
- Fence new upload-alias writes from stopped or unavailable send-ledger owners
  so a delayed media replay cannot overwrite newer receipts after reload.
- Initialize shared private media storage independently of transport enablement.
  Removing a disabled connection preserves other connections' state, and cache
  expiry cleanup remains active after restarting with media disabled.

The HAOS app remains at 0.2.4 with the same pinned GOWS engine and disabled
automatic download defaults. Live HA/WhatsApp verification remains required.

## [1.8.0] - 2026-10-17

### Added

- Add optional schema-v1 attachment descriptors with explicit GOWS native
  voice-note, ordinary-audio and unknown classification, without downloading
  during callback publication.
- Add active-administrator-only `get_media`, authenticated bounded binary
  download and multipart staging routes, shared private cache limits, exact
  conversation/message bindings, fresh authorization, and immutable expiry.
- Add `send_media_to_conversation` for staged images, PDFs and audio, including
  native Ogg/Opus voice transport. Reuse the durable text-send ledger for
  correlation, conflicts, restart uncertainty and replay after byte expiry.
- Publish effective media capabilities, safe errors, synthetic consumer
  fixtures and a separate manual activation checklist.

### Compatibility

- Media transport defaults off. Supported media requires GOWS 2026.9.1 or newer
  and a matching private `WAHA_BASE_URL`; app 0.2.4 prepares that origin fix
  while preserving the existing digest-pinned engine. Keep automatic event
  and API downloads disabled; explicit per-message requests override defaults.
- Existing text, notify, poll, command and group/guest contracts remain.
  Speech processing and consumer policies are outside this integration.
- Source and mock checks are not live Home Assistant/WhatsApp verification.

## [1.7.0] - 2026-10-15

### Added

- Add opt-in, recipient-scoped `message.outbound` observations for configured
  notifications, conversation replies, and actionable polls, with explicit
  content redaction and sanitized observer health. Delivery remains independent
  of observation; arbitrary-phone sends are excluded.
- Add optional durable `client_request_id` correlation to conversation sends,
  safe pending/accepted/failed/unknown outcomes, bounded retention, conflict
  rejection, verified atomic storage, and restart recovery that never blindly
  resubmits uncertain sends. Reload drains admitted sends before replacing
  the request-ledger owner.
- Add explicit, account-bound general group subentries and native notify
  entities, with verified roster identity, opaque participants, and available
  mention/quote metadata. Managed guest lifecycle stays independent.
- Add `get_channel_capabilities`, public communication contracts, sanitized
  fixtures, migration notes, and a live-verification checklist. No receipts,
  media retrieval, replay, execution policy, or agent runtime is introduced.

### Compatibility

- Preserve schema version 1, unkeyed actions, existing notify/poll contracts,
  private registered command recognition, and managed guest safeguards.
- General groups require WAHA GOWS 2026.8.2 or newer. Existing direct send
  paths retain their backend requirements. The HAOS app is unchanged.
- Live HA/WhatsApp verification remains required before activation.

## [1.6.0] - 2026-09-28

### Added

- Add administrator-managed, per-connection text command declarations for
  selected configured contacts or explicitly allowed current guests. Accepted
  private messages publish a structured `command.requested` channel event;
  the integration does not execute Home Assistant actions.

## [1.5.0] - 2026-09-28

### Added

- Add an opt-in managed WhatsApp guest group with reviewed Home Assistant
  administrator mapping, verified group settings, opaque per-stay membership
  events, a group notify entity, and private current-member routes.
- Add a read-only `get_group_membership` action and explicit recovery for an
  uncertain group-create result. Missing private guest state fails closed and
  never triggers automatic group recreation.
- Document the group event/action contract and a manual Home Assistant live
  test checklist. Live Home Assistant/WhatsApp behavior remains unverified.

## [1.4.1] - 2026-09-24

### Added

- Publish a `waha_whatsapp_event` with `type: poll.selection_settled` after a
  valid configured-contact poll choice survives its correction window. Include
  opaque poll and conversation IDs, the selected option, and optional action
  ID; the synthetic "No action" choice is visible with a null action ID.
- Keep the existing `mobile_app_notification_action` event unchanged for real
  action choices. Polls sent before 1.4.1 retain that legacy event but do not
  gain an inferred channel identity after an update.

## [1.4.0] - 2026-09-24

### Added

- Receive authenticated direct-chat messages and reactions from configured
  contacts as structured `waha_whatsapp_event` events with opaque conversation
  and message IDs.
- Send text back to a configured conversation with
  `waha_whatsapp.send_to_conversation`, optionally quoting a tracked message.
- Preserve the existing individual notify entities and poll action event
  contract while extending the private webhook for bidirectional messaging.

## [1.3.3] - 2026-08-23

### Fixed

- Keep individual WhatsApp notify entities available on Home Assistant 2026.8
  by scoping each WAHA device to its recipient config subentry.

## [1.3.2] - 2026-07-28

### Fixed

- Restore actionable poll events on Home Assistant 2026.7 by using Home
  Assistant's default local event origin instead of the unavailable
  `EventOrigin.REMOTE` member.
- Preserve linked Person attribution through `context.user_id`; event origin
  does not affect the attributed user context.
- Add a compatibility regression check preventing poll dispatch from
  depending on `EventOrigin` members that are absent from supported Home
  Assistant releases.

## [1.3.1] - 2026-07-27

### Fixed

- Restore compatibility with Home Assistant 2026.7 by reading the stable
  Person `user_id` state attribute without importing the newer
  `PersonEntityStateAttribute` enum that is unavailable in that release.
- Add a compatibility regression check preventing the integration from
  depending on that unavailable Person module export again.

## [1.3.0] - 2026-07-27

### Added

- Attribute settled WhatsApp poll actions to the active Home Assistant user
  linked to the configured contact's optional Person association.
- Mark WhatsApp poll action events with Home Assistant's remote event origin,
  matching the Companion App webhook contract.

### Privacy and compatibility

- Persist only `person_entity_id` with pending poll correlation. Home Assistant
  user IDs are resolved from the Person when a vote settles and are never
  stored by the integration or added to event data.
- Keep `context.user_id` unset for contacts without a Person, People without a
  linked active Home Assistant user, missing Person entities, and legacy
  pending polls created before this release.

## [1.2.2] - 2026-07-27

### Fixed

- Correlate GOWS poll votes by the engine-stable WhatsApp message token when
  WAHA changes the serialized message-ID chat envelope from `@c.us` on send to
  `@lid` on the vote webhook.
- Resolve webhook LIDs through WAHA's session LID mapping API before comparing
  the voter and poll destination with the configured phone-number chat.
- Canonicalize persisted pending poll IDs during restore so polls created by
  1.2.0 or 1.2.1 remain correlatable after updating.

### Security

- Require WAHA to map every alternate LID to the configured `@c.us` contact.
  Unknown, unmapped, or mismatched LIDs continue to reject the vote without
  firing a Home Assistant event.

## [1.2.1] - 2026-07-27

### Fixed

- Accept GOWS direct-message poll votes when WhatsApp identifies the voter with
  an alternate numeric `@lid`, while continuing to require the authenticated
  webhook, configured session, exact outgoing message ID and direction, and
  exact phone-number destination chat.
- Add sanitized rejection warnings and diagnostic counters so authenticated
  poll votes no longer fail silently when parsing or correlation rejects them.

### Security

- Keep mismatched phone-number JIDs, malformed LIDs, destination changes, and
  group-chat identities rejected. Diagnostics never expose phone numbers,
  message IDs, actions, or poll contents.

## [1.2.0] - 2026-07-27

### Added

- Add single-selection WhatsApp actionable polls through
  `waha_whatsapp.send_poll`, accepting the existing Companion App `action` and
  `title` dictionaries.
- Publish settled WhatsApp choices on the existing
  `mobile_app_notification_action` event so current action-handler automations
  remain compatible.
- Configure a private WAHA poll webhook automatically, with SHA-512 HMAC,
  outbound message/recipient correlation, persisted pending state, and a
  configurable five-second vote-correction window.
- Add a non-triggering fallback choice for notifications with only one real
  action.

### Security

- Preserve unrelated WAHA session webhooks while managing the integration's
  callback, and keep webhook credentials and correlation data private and
  redacted from diagnostics.

### Behavior

- Keep workflow execution, state guards, semantic expiry, and confirmation
  messages in Home Assistant automations. Failed/undecodable votes never fire
  an action or send an automatic response.

## [1.1.0] - 2026-07-24

### Changed

- Make the Home Assistant Person association optional identity metadata and
  allow individual contacts to use an independently configured display name.
- Expose the associated `person_entity_id` as non-sensitive state metadata on
  individual notify entities so routers can discover contacts dynamically.
- Keep notification routing entirely contact-based. A possible broadcast to
  all configured contacts is explicitly separate and remains unimplemented.

### Removed

- Remove Family, Adults, and Guests recipient settings and fan-out notify
  entities.

### Migration

- Migrate config entries to version 1.2 by removing only obsolete group fields
  and the three exact integration-owned group entities while preserving every
  individual contact, Person association, and individual notify entity.

## [1.0.1] - 2026-07-21

### Fixed

- Reload the integration automatically after recipients are added, updated, or
  removed so individual entities and household group membership stay current.
- Use the stable shared device name `WAHA` so new entity IDs are generated as
  `notify.waha_<recipient>` and `notify.waha_<group>` instead of including the
  account name and WAHA version.
- Avoid duplicate config-entry reloads on Home Assistant 2026.6 and newer by
  routing entry changes through one update listener.

### Documentation

- Correct the notify entity examples and add a staged Companion App-to-WhatsApp
  migration guide, including actionable-notification and fallback guidance.

## [1.0.0] - 2026-07-21

### Added

- A WAHA-native HACS integration for free-form outbound WhatsApp messages.
- Automatic Supervisor discovery between the HAOS app and HACS integration.
- Recipient subentries that select existing Home Assistant Person entities and
  associate them with WhatsApp phone numbers.
- Native individual, Family, Adults, and Guests notify entities.
- A direct `waha_whatsapp.send_message` action for arbitrary phone numbers.
- Redacted diagnostics and manual support for externally hosted WAHA servers.

### Changed

- Replaced the Kapso Cloud API and template system with the self-hosted WAHA
  API and linked-device session.
- Renamed the integration domain from `kapso_whatsapp` to `waha_whatsapp`.
- Renamed and refocused the repository as Home Assistant WhatsApp.

### Removed

- Kapso credentials, approved templates, authentication templates, and the
  24-hour free-form messaging restriction.

### Migration

- This is an intentional breaking provider migration. Remove the Kapso
  integration before installing WAHA WhatsApp and re-add recipient mappings.
- The complete Kapso v0.2 state remains on the `legacy-kapso` branch and the
  original `v0.1.0` and `v0.2.0` tags remain unchanged.

[1.0.0]: https://github.com/sebastian-greco/home-assistant-whatsapp/releases/tag/v1.0.0
[1.0.1]: https://github.com/sebastian-greco/home-assistant-whatsapp/releases/tag/v1.0.1
[1.1.0]: https://github.com/sebastian-greco/home-assistant-whatsapp/releases/tag/v1.1.0
[1.2.0]: https://github.com/sebastian-greco/home-assistant-whatsapp/releases/tag/v1.2.0
[1.2.1]: https://github.com/sebastian-greco/home-assistant-whatsapp/releases/tag/v1.2.1
[1.2.2]: https://github.com/sebastian-greco/home-assistant-whatsapp/releases/tag/v1.2.2
[1.3.0]: https://github.com/sebastian-greco/home-assistant-whatsapp/releases/tag/v1.3.0
[1.3.1]: https://github.com/sebastian-greco/home-assistant-whatsapp/releases/tag/v1.3.1
[1.3.2]: https://github.com/sebastian-greco/home-assistant-whatsapp/releases/tag/v1.3.2
[1.3.3]: https://github.com/sebastian-greco/home-assistant-whatsapp/releases/tag/v1.3.3
[1.4.0]: https://github.com/sebastian-greco/home-assistant-whatsapp/releases/tag/v1.4.0
[1.4.1]: https://github.com/sebastian-greco/home-assistant-whatsapp/releases/tag/v1.4.1
[1.5.0]: https://github.com/sebastian-greco/home-assistant-whatsapp/releases/tag/v1.5.0
[1.6.0]: https://github.com/sebastian-greco/home-assistant-whatsapp/releases/tag/v1.6.0

[1.9.0]: https://github.com/sebastian-greco/home-assistant-whatsapp/releases/tag/v1.9.0
[1.10.0]: https://github.com/sebastian-greco/home-assistant-whatsapp/releases/tag/v1.10.0
