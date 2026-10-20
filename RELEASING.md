# Releasing

The repository contains two independently versioned deliverables:

- the WAHA WhatsApp HACS integration, versioned in `pyproject.toml` and
  `custom_components/waha_whatsapp/manifest.json`;
- the WAHA HAOS app, versioned in `waha/config.yaml` and its Docker build
  argument, then published to GHCR with the same image tag.

Integration releases use `vMAJOR.MINOR.PATCH`. App releases use
`waha-vMAJOR.MINOR.PATCH`. Never move or reuse a published tag.

Every push to `main` that changes `waha/` builds and publishes the app image.
Pull requests build it without publishing. Bump the app version whenever its
runtime image changes; documentation-only edits do not require a bump.

## Release procedure

1. Update the integration and/or app versions in all matching metadata files.
2. Update the root and/or app changelog.
3. Run the validation commands from `README.md`.
4. Commit and push to `main`, then wait for all validation and image builds.
5. Publish the app tag first when both deliverables changed:

   ```bash
   git tag -a waha-v0.2.0 -m "Release WAHA app 0.2.0"
   git push origin waha-v0.2.0
   ```

6. Publish the HACS integration tag last so it remains GitHub's latest release:

   ```bash
   git tag -a v1.0.0 -m "Release WAHA WhatsApp 1.0.0"
   git push origin v1.0.0
   ```

The release workflow verifies each tag against the appropriate version file
before creating the GitHub release.

## Media transport 1.8.0 preparation

The source prepares integration 1.8.0 and app 0.2.4. The app advertises its
matching private media origin; the digest-pinned GOWS 2026.9.1 engine is unchanged.
Source validation and synthetic fixtures do not
establish a live WhatsApp test. Review [media requirements](docs/MEDIA_API.md)
and complete the [activation checklist](docs/MEDIA_HA_TEST.md) separately.
Do not publish a release tag or deploy merely because source checks passed.

## Voice-note compatibility 1.8.1 preparation

Integration 1.8.1 accepts exactly bound incoming GOWS PTT with complete
Ogg/Opus packets/pages but no final EOS flag, fences retired ledger writes,
and restores cache expiry while media is disabled. App 0.2.4 and its pinned engine
are unchanged, so this patch requires only an integration release tag.
Complete the normal source validation and publication steps above; the
[fix handoff](docs/VOICE_EOF_FIX.md) separates synthetic checks from the
remaining HA update and fresh phone test.

## Application-owned polls 1.9.0

Only the HACS integration changes. App 0.2.4 and its pinned engine remain
unchanged; do not publish an app tag or rebuild the app for this release.
Validate the integration and matching version metadata, push to `main`, wait
for its validation checks, then push the new annotated `v1.9.0` tag. The tag
workflow creates the latest integration release for HACS.

The release adds generic keyed poll transport and bounded selection recovery.
Consumers own all action semantics and should recover on reconnect and apply
durable event deduplication. Legacy household action events are unchanged.
Source publication does not install the integration, reload configuration,
restart Home Assistant or update consumer agents.

## Read receipts and typing preparation

This source change prepares generic exact-message read receipts and transient
typing for the HACS integration. The HAOS app and its pinned GOWS 2026.9.1
engine are unchanged. This remains an unversioned Unreleased preparation; no
integration or app metadata bump is planned here. Activity capabilities are
advertised only for a verified, connected supported GOWS session. Unsupported
activity does not disable ordinary message transport. Review the
[activity API and fixtures](docs/ACTIVITY_API.md) and run the source validation
commands above. Complete its manual test checklist on a non-production
connection before activation. Source checks do not confirm read-receipt
privacy behavior or phone notification cleanup.

Integration metadata remains 1.9.0 and app metadata remains 0.2.4; choose the
next integration version before publication. No app release or engine change
is needed for this feature.

Source validation on 2026-10-20:

- Full Python suite with the existing Home Assistant dependencies: **751
  passed, no skips**. CI-style environment without Home Assistant: **678 passed,
  23 skipped** for optional Home Assistant checks.
- Node suite: **20 passed**. Ruff, Python compilation, matching integration/app
  version checks, Node syntax, fixture/schema compatibility and whitespace
  checks passed.
- Independent **Astra High** review found two P2 issues: first-account binding
  after startup and guest membership provenance across an awaited lookup. Both
  were fixed with regressions. Astra High's follow-up ran **98 tests** and found
  no remaining actionable findings.

These checks used synthetic data and local fake servers. Live read/privacy,
presence expiry, reconnect and phone notification checks remain outstanding;
publication and production activation require separate authorization.
