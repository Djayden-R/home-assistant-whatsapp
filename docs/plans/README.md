# Implementation plans

Plans preserve design intent, rationale, and verification criteria; they are
not user recipes or the normative current API. Check each plan's status and
the linked user/API references before relying on a capability. Proposed
fields/actions are not proof of what is implemented or released.

## Next priority

- [Managed guest group](MANAGED_GUEST_GROUP.md): one opt-in WhatsApp guest
  group, persistent participant/membership identities, membership events,
  group notifications, and private communication with current members.
  Status: included in integration version 1.5.0, but not live-verified. Use
  the normative [user guide](../GUEST_GROUPS.md),
  [API reference](../GUEST_GROUP_API.md),
  [automation guide](../GUEST_GROUP_AUTOMATIONS.md), and
  [live test checklist](../GUEST_GROUP_HA_TEST.md) for current contracts.
- [WAHA group capabilities](WAHA_GROUP_CAPABILITIES.md): historical
  version-specific prerequisite research. The bundled app source now pins
  GOWS 2026.9.1; the required API floor remains 2026.8.2. Neither source
  inspection nor unit tests substitute for the manual live test.

The generic [media transport](../MEDIA_API.md) extends those routes in 1.8.0.
Voice transcription, command execution, access provisioning, and sidebar
improvements are not part of this transport implementation.

## Earlier design records

- [v1.4 implementation plan](../V1_4_0_PLAN.md)
- [WhatsApp channel architecture](../WHATSAPP_CHANNEL_DESIGN.md)

Keep historical plans in place. Link new work here and update the relevant
user-facing reference when it actually ships.
