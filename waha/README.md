# WAHA for Home Assistant

Self-hosted WAHA packaged as a Home Assistant app, with a small ingress-native control panel for the Home Assistant sidebar.

This experimental app release provides:

- pinned WAHA 2026.9.1 using the browserless GOWS engine;
- persistent WhatsApp session data under the app's `/data` volume;
- automatic creation and restoration of one household session;
- QR linking plus start, stop, and restart controls in the sidebar;
- Home Assistant watchdog health checks and cold backups;
- private Supervisor discovery for the companion HACS integration;
- a private API key and no host port exposure by default.
- a media URL base matching the private Supervisor discovery hostname, while
  keeping automatic media downloads disabled by default.

See `DOCS.md` for installation and security notes.
