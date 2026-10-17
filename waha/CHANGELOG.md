# Changelog

## 0.2.4

- Set WAHA's generated media URL base to the same private add-on hostname the
  integration discovers. The API still binds to `0.0.0.0`; file URLs no longer
  inherit that listener-only address. The standalone fallback is loopback.
- Keep WAHA event/API automatic media download defaults disabled unless the
  existing `download_media` option is explicitly enabled. On-demand retrieval
  can therefore use the message-level `downloadMedia=true` override.

## 0.2.3

- Upgrade the bundled browserless WAHA GOWS engine to 2026.9.1. Upstream
  releases add group member-add and membership-approval APIs and improve
  phone-number/LID handling; these are engine capabilities, not a new
  Home Assistant guest/group integration. See the official
  [2026.7.2](https://github.com/devlikeapro/waha/releases/tag/2026.7.2),
  [2026.8.1](https://github.com/devlikeapro/waha/releases/tag/2026.8.1),
  [2026.8.2](https://github.com/devlikeapro/waha/releases/tag/2026.8.2), and
  [2026.9.1](https://github.com/devlikeapro/waha/releases/tag/2026.9.1)
  release notes.
- The companion HACS integration has a separate source/version lifecycle;
  installing or upgrading this app does not deploy integration changes.
- Apply the media-download option explicitly to WAHA's new event and API
  defaults, preserving disabled downloads by default.
- Add startup/shutdown and credential-redaction regressions plus a real-image,
  isolated-session smoke check that gates app image publication.

## 0.2.2

- Prevent Home Assistant ingress root requests containing a double slash from
  crashing the WAHA control panel and stopping the app.

## 0.2.1

- Fix saving the app configuration by using Home Assistant Supervisor's
  supported string schema for the device name.
- Enforce the device name's 1–64 character limit when the app starts.

## 0.2.0

- Publish the app's internal WAHA host, session, and API credentials through
  Home Assistant Supervisor discovery.
- Enable the WAHA WhatsApp HACS integration to connect without exposing port
  3000 or duplicating credentials manually.

## 0.1.1

- Replace the obsolete Supervisor watchdog field with a native Docker health
  check.
- Remove configuration values that duplicate Home Assistant defaults.

## 0.1.0

- Initial experimental HAOS app.
- Add the WAHA 2026.7.1 GOWS engine.
- Add persistent single-session setup.
- Add an ingress-native sidebar control panel with QR and lifecycle controls.
- Keep the WAHA API port disabled by default.
