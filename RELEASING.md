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

## Application poll compatibility 1.10.1

Integration **1.10.1** fixes application-owned poll destination binding by
recording the normalized destination used by the transport and strictly
canonicalizing supported phone-number JIDs. The poll destination and voter are checked
independently; alternate LIDs remain dependent on WAHA's verified mapping.
During restore, affected pending application polls from the earlier
implementation have only their destination binding repaired. The vote
journal, settling state, timestamps, origin, actions, schema, replay data, and
opaque identities are preserved. A vote rejected before journaling is not
recreated; a fresh genuine vote or supported text/voice response is needed.
Legacy automation-owned poll handling and the consumer API are unchanged.

Only the HACS integration changes. `pyproject.toml`, the integration manifest,
and the root package in `uv.lock` must all report **1.10.1**. HAOS app 0.2.4
and its pinned GOWS 2026.9.1 engine remain unchanged, so no app image or app
tag is required. Run the normal validation above and verify the matching tag
metadata:

```bash
uv run python scripts/check_version.py v1.10.1
uv run python scripts/check_version.py waha-v0.2.4
uv lock --check
```

After integration changes are on `main` and CI passes, publish the annotated
`v1.10.1` integration tag using the general procedure above. Publication does
not install the integration, replay rejected votes, reload configuration, or
activate a consumer. Source and synthetic checks do not establish live
WhatsApp behavior; verify poll votes on a test connection before production
activation.

Source validation on 2026-10-21:

- Full Python suite with Home Assistant dependencies: **782 passed, no skips**.
  CI-style environment: **709 passed, 23 optional Home Assistant skips**.
- Node suite: **20 passed**. Ruff, Python compilation, version/tag consistency,
  lockfile consistency, Node syntax and whitespace checks passed.
- Added **31 synthetic regressions**, including production send, registration,
  signed webhook dispatch, settling timers, persisted-record repair, original
  vote timestamps, journal bounds, duplicate suppression and restart/replay.
  The send roundtrip and persisted-repair regressions fail against the original
  implementation.
- Independent **Astra High** review found no remaining actionable findings.
  Source tests do not establish that the particular live incident is resolved;
  one genuine vote and its consumer continuation still need live verification.

## Poll timestamp compatibility and settlement recovery (unreleased)

The current source work addresses the pinned GOWS 2026.9.1 poll vote
timestamp: WAHA passes protocol `senderTimestampMS` into numeric
`poll.vote.vote.timestamp` unchanged. The previous public settlement path
treated this value as seconds and failed while constructing its UTC event time.
The compatibility rule preserves seconds-style documented inputs, normalizes
supported millisecond values to canonical seconds, and marks newly persisted
pending vote timestamps with their unit. Both recognized numeric forms use a
shared decoded binary-float seconds value, rounded to microseconds with
`Decimal.from_float` and ties-to-even. Published journals remain unchanged;
ambiguous early-epoch millisecond values retain the legacy seconds
interpretation. Other engine timestamp units are not generalized; see the
[poll timestamp/recovery note](docs/POLL_SELECTION_RECOVERY.md).

The issue was reproduced before the fix by running the synthetic public
settlement journey against the original `polls.py` and `poll_manager.py` from
commit `3cbc713`. From the repository root, this command initializes the
existing isolated test modules, substitutes those two baseline source files in
memory, and runs only the millisecond public journey. It does not check out,
write, or modify the baseline commit:

```bash
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=.ha-runtime/lib/python3.14/site-packages .venv/bin/python - <<'PY'
import importlib, subprocess, pytest
importlib.import_module('tests.test_poll_settlement')
from tests._loader import load_integration_module
for name in ('polls', 'poll_manager'):
    source = subprocess.check_output(['git', 'show', f'3cbc713:custom_components/waha_whatsapp/{name}.py'], text=True)
    module = load_integration_module(name)
    exec(compile(source, f'baseline_3cbc713_{name}.py', 'exec'), module.__dict__)
raise SystemExit(pytest.main(['-q', '-p', 'no:cacheprovider', 'tests/test_poll_settlement.py::test_gows_millisecond_vote_real_public_journey_survives_timer_and_restart[milliseconds]', '--tb=short']))
PY
```

On the baseline this reaches the settlement timer and fails with
`ValueError: year 61975 is out of range` in `datetime.fromtimestamp`; it fails
before publishing the settled event. The same regression against the current
source passes:

```bash
PYTHONPATH=.ha-runtime/lib/python3.14/site-packages .venv/bin/pytest -q -p no:cacheprovider 'tests/test_poll_settlement.py::test_gows_millisecond_vote_real_public_journey_survives_timer_and_restart[milliseconds]' --tb=short
```

Validation on 2026-10-21: the author’s focused HA poll/channel run passed 161
tests; the full HA-runtime suite passed **842**, the plain CI-style suite passed
**764 with 28 optional Home Assistant skips**, and Node passed **20**. Ruff,
version consistency (integration `1.10.1`, app `0.2.4`), Python compilation,
offline lock consistency, Node syntax, synthetic JSON and local documentation
links, and `git diff --check` passed. The plain suite showed an existing
aiohttp `NotAppKeyWarning`; the HA-runtime suite also showed an aiohttp
application-inheritance deprecation warning. An initial sandboxed run had 27
loopback socket permission failures; the authorized local rerun passed.

The final tracked diff and three new text files passed a targeted six-pattern
credential scan (private keys, AWS, GitHub, OpenAI, Slack, and credential-bearing
URLs), with zero matches. Manual privacy review found only synthetic example
identities. No dedicated secret-scanner executable was installed locally.

Independent Astra High A/D and B/C follow-ups ended clean with no remaining
actionable findings. A/D independently exercised 161 focused HA tests and
30,000 numeric inputs; B/C exercised a 60-case settlement follow-up. The
review identified and the source fixed: a legacy action lost after an
acknowledged cancellation, a registration race during unload, a one-microsecond
rounding mismatch between equivalent seconds and milliseconds, and overly
strong wording about the exact pinned provider implementation. These review
checks are not live-service verification.

HACS, hassfest, and add-on linter were not run because they are not available
locally; no hosted workflow was triggered. No live HA/WhatsApp check has been
performed. Exact pinned private GOWS dependency behavior and precision, a live
webhook payload, other engines' timestamp units, and live selection/replay
behavior remain unverified. No version metadata was changed as part of this
unreleased source work.

After separately authorized activation of the patched release and confirmation
that the patched code is loaded (the manifest version alone cannot distinguish
this unreleased source revision), first query selection replay for the same
conversation from the consumer's last durable sequence. If the original vote
is present, continue that same pending request and question with its saved
event identity and vote time, and check for duplicate effects. If no recoverable
original vote exists, use a fresh genuine vote on the still-supported poll or
a supported genuine text/voice answer. Do not reset or replace the consumer
request as a permanent recovery path, and do not replay or synthesize a
previously rejected vote. No live check has been performed.

## Read receipts and typing 1.10.0

Integration **1.10.0** adds generic exact-message read receipts and transient
typing through the HACS integration. Version metadata is synchronized in
`pyproject.toml`, `custom_components/waha_whatsapp/manifest.json`, and the root
project entry in `uv.lock`. Only the integration changes: HAOS app 0.2.4 and its
pinned GOWS 2026.9.1 engine stay unchanged, so no app image or app tag is
required.

The activity capabilities are advertised only for a verified, connected GOWS
2026.9.1 or newer session. Unsupported activity leaves ordinary message
transport available. See the [activity API and fixtures](docs/ACTIVITY_API.md)
for service schemas, bounds, outcomes, and live checks.

Run the normal validation commands above, including these release tag checks:

```bash
uv run python scripts/check_version.py v1.10.0
uv run python scripts/check_version.py waha-v0.2.4
```

Once the changes are on `main` and CI passes, publish the integration with the
annotated `v1.10.0` tag using the general procedure above. Do not publish an app
tag for this change. Publishing the integration does not install it or
activate a consumer. The manual live checks for WhatsApp read privacy,
transient presence expiry, reconnect behavior, and phone notification cleanup
remain outstanding before production activation.

Source validation on 2026-10-20:

- Full Python suite with the existing Home Assistant dependencies: **751
  passed, no skips**. CI-style environment without Home Assistant: **678
  passed, 23 skipped** for optional Home Assistant checks.
- Node suite: **20 passed**. Ruff, Python compilation, version checks, Node
  syntax, fixture/schema compatibility, and whitespace checks passed.
- Independent **Astra High** review found two P2 issues: first-account binding
  after startup and guest membership provenance across an awaited lookup. Both
  were fixed with regressions. Astra High's follow-up ran **98 tests** and found
  no remaining actionable findings.

These checks used synthetic data and local fake servers. They do not verify
live WhatsApp read/privacy behavior, presence expiry, reconnects, or phone
notification cleanup.
