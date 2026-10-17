# WAHA for Home Assistant

## Before starting

Create a long random API key (at least 16 characters). A password manager-generated 32-character value is ideal. The app privately publishes this key to the companion Home Assistant integration through Supervisor discovery; it is never displayed by the discovery flow.

## First start

1. Set `api_key` in the Configuration tab and save.
2. Start the app.
3. Enable **Show in sidebar** if Home Assistant did not enable it automatically.
4. Open **WAHA** in the sidebar.
5. When the session reports `SCAN_QR_CODE`, select **Show QR**.
6. On the phone holding the dedicated house WhatsApp account, open **Linked devices**, select **Link a device**, and scan the QR.
7. Wait for the status to become `WORKING`.

## Connect the Home Assistant integration

Install **WAHA WhatsApp** through HACS using the repository URL
`https://github.com/sebastian-greco/home-assistant-whatsapp`, then restart Home
Assistant. Restart this app once if the discovered integration is not already
visible under **Settings → Devices & services**. Confirm the discovered card,
then add recipients by selecting existing Home Assistant Person entities and
entering their WhatsApp phone numbers.

The API port can remain disabled. Home Assistant connects over the private app
network using credentials passed through Supervisor discovery.

The app persists the linked session and restarts it after Home Assistant or app restarts. A cold backup stops the app briefly so the session database is copied consistently.

## Upgrade the app

Before updating to app 0.2.4, create a cold backup of the WAHA app and confirm
it includes the app's `/data` session data. The cold backup briefly stops WAHA
so its database is copied consistently. Keep that backup until the upgraded
session has been checked.

Update the app in Home Assistant; the app restart is part of the update. Do not
uninstall the app, delete or clear `/data`, log out the WhatsApp session, or
change its session name or API key as an upgrade step. The update keeps the
existing `/data` volume and configured API key, and reuses the stored session
data rather than intentionally clearing or relinking it. App 0.2.4 keeps the
pinned GOWS 2026.9.1 engine and sets the generated media URL base to the same
private hostname published through Supervisor discovery. The HACS integration
has a separate source/version lifecycle; installing this app update does not
install or deploy integration changes.

After the app starts, open the WAHA sidebar panel and check that its version is
2026.9.1, the session reaches `WORKING`, and the sidebar panel loads. Confirm
the discovered **WAHA WhatsApp** integration is still connected. Then use the
integration to send a harmless test message and a new actionable poll; select
its test option and verify the response event. Do not reuse an old poll.
These are upgrade checks for the HA owner, not a claim that a paired-session
test has been run.

For on-demand media retrieval, app 0.2.4 sets `WAHA_BASE_URL` to
`http://<Supervisor-discovered-host>:3000` (underscores in `HOSTNAME` are
normalized to hyphens); this matches the private host the integration uses
even though WAHA binds its HTTP listener to `0.0.0.0`. Keep
`download_media` false so event/API defaults stay disabled; exact-message
retrieval applies `downloadMedia=true` per request. Separately managed WAHA
installations need the same reachable base URL and version/engine checks.

If rollback is needed, use the Home Assistant cold-backup restore procedure
with the pre-update app backup that includes session data and configuration.
Restore behavior has not been validated during this source change.

Before app image publication, CI must build the image and pass an isolated,
unlinked Docker-session startup and configuration-persistence smoke test. Do
not use a linked account or HA data for that test. A local Docker daemon was
unavailable during this source work, so no local image smoke test was run.
This check does not verify migration of a linked WhatsApp account. Developers
can repeat the smoke test with
`node scripts/smoke_waha_app.mjs IMAGE_TAG 2026.9.1` from the repository root;
it creates and removes its own test container/volume and never uses HA data.

## Controls and logs

The sidebar control panel covers the normal household workflow: health, version, QR, and session start/stop/restart. Use **Settings → Apps → WAHA** to start or stop the whole container and inspect full logs.

WAHA's native dashboard does not currently understand Home Assistant's tokenized ingress base path. It is therefore not the default sidebar UI. If advanced debugging is temporarily necessary, map container port `3000/tcp` to an unused host port in the Network section, then open `http://HOME_ASSISTANT_IP:PORT/dashboard`. Sign in as `admin` and use the configured API key as the password. Remove the port mapping afterward.

The prebuilt `ghcr.io/sebastian-greco/ha-waha` package is public so fresh HAOS
installations can pull it without registry credentials.

## Security

- Do not forward WAHA port 3000 from your router or expose it through a public reverse proxy.
- Keep the app sidebar restricted to Home Assistant administrators.
- Keep `download_media` disabled when using per-message retrieval; enabling it
  turns on automatic event/API downloads.
- Store the dedicated eSIM in a recoverable phone, enable WhatsApp two-step verification, and add a recovery email.
- WAHA uses an unofficial WhatsApp protocol. A dedicated number reduces impact but does not remove the risk of account restriction.

## Resource use

The app uses the browserless GOWS engine. Upstream GOWS group-member and
phone-number/LID improvements do not add a separate Home Assistant guest/group
integration or group-management UI to this app. WAHA's own guidance estimates
roughly 200 MB RAM for one GOWS session. Actual use varies during login,
synchronization, and message activity.
