# Poll vote timestamp and recovery note

This note documents the application-owned poll compatibility fix included in
integration **1.10.2**. It does not change the poll service or event schema, add
consumer policy, or establish that the live WhatsApp session was verified. The
HAOS app remains at 0.2.4 with its existing pinned GOWS 2026.9.1 engine.

## Timestamp source and compatibility

In WAHA 2026.9.1, GOWS's `toPollVotePayload` copies
`event.Message.pollUpdateMessage.senderTimestampMS` directly into
`payload.vote.timestamp`. The pinned WAHA configuration selects
`devlikeapro/gows-plus` ref `v1.0.47`. The field name and WAHA DTO's “Timestamp,
ms” description support treating this integration path as Unix milliseconds.
Upstream WhatsMeow `main` constructs its corresponding protocol field with
`time.Now().UnixMilli()`, which corroborates the unit; it does not independently
verify the exact pinned GOWS dependency implementation, its numeric precision,
or a live webhook payload:

- [WAHA GOWS poll-vote mapping, pinned 2026.9.1](https://github.com/devlikeapro/waha/blob/2026.9.1/src/core/engines/gows/session.gows.core.ts#L2878-L2908)
- [WAHA GOWS source pin](https://raw.githubusercontent.com/devlikeapro/waha/2026.9.1/waha.config.json)
- [WhatsMeow `SenderTimestampMS` construction](https://github.com/tulir/whatsmeow/blob/main/msgsecret.go#L398-L419)

The prior settlement path interpreted that number as Unix seconds and passed
it to `datetime.fromtimestamp`. The known baseline public settlement path
therefore failed before publishing its settled-selection event for a GOWS
millisecond value. A synthetic regression reproduced the baseline failure:
`datetime.fromtimestamp` raised `ValueError: year 61975 is out of range` when
the settlement timer advanced. The same public journey passes against the
current source.

WAHA's public material is inconsistent: the pinned poll DTO labels the field
“Timestamp, ms” but shows a seconds-sized example, and the generic event guide
shows another seconds-sized value. The outer webhook `timestamp` is a separate
field and is not the poll vote time. Seconds-shaped examples are kept as
compatibility input, not treated as evidence that the GOWS source emits
seconds.

- [Pinned WAHA `PollVote` DTO](https://github.com/devlikeapro/waha/blob/2026.9.1/src/structures/webhooks.dto.ts#L68-L80)
- [WAHA generic `poll.vote` example](https://waha.devlike.pro/docs/how-to/events/)

Other WAHA engine paths do not support a blanket unit claim:

| WAHA engine path | Source behavior | Unit conclusion |
| --- | --- | --- |
| GOWS 2026.9.1 | Copies the field named `senderTimestampMS` into `vote.timestamp` | Treat as milliseconds for this supported path; exact pinned dependency semantics are not independently verified |
| NOWEB successful `poll.vote` | Calls `ensureNumber(pollUpdate.senderTimestampMs)` without conversion | Milliseconds by field provenance; `poll.vote.failed` instead uses `message.messageTimestamp`, and the source comments on changing that path |
| WEBJS `poll.vote` | Copies `vote.interractedAtTs` without conversion; upstream PollVote maps it from `data.senderTimestampMs` | Millisecond field lineage, not a cross-engine integration guarantee |
| WPP `poll.vote` | Copies `data.timestamp` without conversion or unit annotation | Uncertain; do not infer its unit |

Sources: [NOWEB successful and failed vote paths](https://github.com/devlikeapro/waha/blob/2026.9.1/src/core/engines/noweb/session.noweb.core.ts#L3347-L3422), [WEBJS vote mapping](https://github.com/devlikeapro/waha/blob/2026.9.1/src/core/engines/webjs/session.webjs.core.ts#L2541-L2588), [upstream WEBJS PollVote field mapping](https://docs.wwebjs.dev/structures_PollVote.js.html), and [WPP vote mapping](https://github.com/devlikeapro/waha/blob/2026.9.1/src/core/engines/wpp/session.wpp.core.ts#L2136-L2177). WAHA pins WEBJS to a dated fork branch; WPP dependencies refer to mutable branches, which further limits conclusions about those exact builds.

The normalization rule is deliberately narrow:

1. Reject booleans, non-numeric, non-finite, negative, and non-representable
   timestamps from settlement.
2. Treat a persisted `timestamp_unit: "seconds"` marker as authoritative; never
   rescale a marked timestamp.
3. Interpret unmarked values within the representable Unix-seconds range as
   seconds, preserving the prior storage convention and seconds-style examples.
4. For a larger unmarked value, accept milliseconds only when division by
   1,000 yields a representable UTC datetime. Both recognized forms first use
   the same decoded binary-float seconds value; `Decimal.from_float` then rounds
   to microsecond precision using ties-to-even. Validate the final UTC datetime
   and mark newly saved pending vote timestamps as `seconds`. This makes
   equivalent millisecond and seconds forms idempotent at the supported
   precision while respecting the precision of the decoded JSON number.
5. Keep ambiguous early-epoch millisecond values that also fit the legacy
   seconds range as seconds. Without persisted provenance, early-millisecond
   recovery cannot be promised.

No arbitrary age or future-skew window is added for compatibility. This rule
does not select units for every engine. Already-published selection journal
entries remain unchanged. The storage envelope stays at version 1; the pending
row's unit marker is additive, and the consumer event remains schema version 1.
Invalid vote times are never replaced with the current time. If a pending vote
timestamp, selected option, or settlement deadline is invalid, the implementation
retains the registered poll and its valid options where possible, clears unsafe
vote metadata, and marks
that timestamp invalid for aggregate diagnostics. A fresh genuine vote can
still be handled for that poll.

## Synthetic compatibility example

The [fixture](examples/poll-selection-recovery-v1.json) is synthetic. It shows
a GOWS timestamp of `1893578400000` milliseconds becoming
`2030-01-02T10:00:00+00:00`; a settled event two seconds later retains that
original time in `vote_occurred_at`. It also shows `1692861427` as a
seconds-style compatibility value, matching the shape of WAHA's generic
example. No live event or account data is included.

## Persistence, recovery and delivery

The consumer API remains unchanged. A settlement is saved before it is made
visible as a public event or through `get_poll_selections`. A bounded Home
Assistant timer retries recoverable construction or persistence failures while
the original pending poll remains available. There are at most three delayed
retries at 1, 5, and 30 seconds for the current accepted vote, with no more
than 256 recovery records per loaded entry. After exhaustion, there is no
continuing retry timer; the pending recovery record remains retained until a
newer accepted vote or reload, and a newer vote starts a fresh retry sequence.
Startup/reload recovery applies only to vote and settling state that were
genuinely persisted; it cannot recover a vote rejected before storage. On
reload, persisted selected work can be scheduled again.

If saving a newly accepted answer fails, the newest answer remains in memory
for bounded recovery, but its survival across process loss is not guaranteed.
The last successfully saved pending vote or receipt is the recoverable disk
state. A construction failure does not establish that the previously saved
original vote was lost.

After a settlement is durably saved, a missed Home Assistant event-bus delivery
can be recovered through the existing selection query with its saved event ID
and sequence. Event-bus delivery is best effort; consumers should durably
deduplicate by event identity and sequence. This is not an exactly-once bus
delivery promise and does not automatically re-publish a saved event. Existing
published journal entries are not rewritten during timestamp normalization.
The existing seven-day, 2,048-receipt journal bounds remain unchanged; these
replay bounds do not impose a new age limit on legacy automation polls.

Previously rejected votes are not fabricated or replayed from guessed input.
Use a fresh genuine vote or a currently supported text/voice response. Do not
edit Home Assistant's stored poll data or synthesize webhook or event-bus votes.

Poll diagnostics expose aggregate counters under
`actionable_polls.settlement`, without vote IDs, raw message content or routing
identities. The exact settlement keys are `construction_failures`,
`persistence_failures`, `publication_failures`, `retry_exhausted`,
`recovery_pending`, `scheduled_work`, `invalid_pending_timestamps`, and
`invalid_pending_records`. The separate `rejection_reasons` map contains
bounded non-sensitive vote-rejection categories. `recovery_pending` includes
exhausted retained records and therefore does not mean that a retry is
scheduled; `scheduled_work` counts active deadlines. `retry_exhausted` counts
retry exhaustion and refusal at the recovery-capacity bound. A representative
synthetic aggregate with a construction failure and no rejected vote is in the
fixture. Rejected-vote log entries contain only a bounded reason category and
state that no Home Assistant action fired; settlement failure logs identify
only the failed phase.

## Status and live verification

Validation on 2026-10-21 passed. The author’s focused HA poll/channel run passed
161 tests. The full HA-runtime suite passed 842 tests; the plain CI-style suite
passed 764 tests with 28 optional HA skips. The Node suite passed 20 tests.
Ruff, version consistency at test time (`1.10.1` integration and `0.2.4` app),
Python compilation, offline lock consistency, Node syntax, JSON fixture and local
documentation-link checks, and `git diff --check` passed. The plain suite emits
an existing aiohttp `NotAppKeyWarning`; the HA-runtime suite additionally emits
an aiohttp application-inheritance deprecation warning. An initial sandboxed
plain-suite attempt had 27 local socket permission failures; the authorized
loopback rerun passed, so those failures were environmental rather than code
failures.

After the source validation, release metadata and the lockfile were updated to
integration 1.10.2 and checked against the `v1.10.2` integration and
`waha-v0.2.4` app version arguments. No app image update is needed.

Independent Astra High A/D and B/C follow-ups finished with no remaining
actionable findings. Review follow-ups fixed loss of a legacy action after an
acknowledged cancellation, an unload-time application poll registration race,
fractional-millisecond rounding disagreement, and an overstatement of pinned
provider-source provenance. A/D independently ran 161 focused HA tests and
30,000 numeric cases; B/C ran a 60-case settlement follow-up. A root-run
baseline comparison is reproducible with the command recorded in
[`RELEASING.md`](../RELEASING.md): it loads the original `polls.py` and
`poll_manager.py` from commit `3cbc713` into isolated test modules, then runs
the public millisecond journey. It fails at timer settlement with the year
61975 `ValueError`; the same test against the current source passes.

The full local test and static checks were run with these commands from the
repository root:

```bash
.venv/bin/pytest --tb=short -p no:cacheprovider
PYTHONPATH=.ha-runtime/lib/python3.14/site-packages .venv/bin/pytest --tb=short -p no:cacheprovider
node --test waha/tests/*.test.mjs
.venv/bin/ruff check .
.venv/bin/python scripts/check_version.py
.venv/bin/python -m compileall -q custom_components tests
UV_CACHE_DIR=/private/tmp/hal-poll-uv-check uv lock --check --offline
node --check waha/run.mjs
node --check waha/control.mjs
node --check waha/discovery.mjs
git diff --check
```

The JSON fixture, its diagnostic aggregate keys, and relative Markdown links
were checked with a local validation script. The exact focused public regression
command appears in [`RELEASING.md`](../RELEASING.md).

These are local synthetic and static checks. As of the 2026-10-21 source-only
validation, hosted HACS, hassfest, and add-on linter checks had not run. Fresh
main CI must pass before release publication; no result for that run is claimed
yet. No live Home Assistant session or WhatsApp phone behavior has been tested,
and the exact pinned private GOWS dependency implementation, numeric precision,
live webhook payload, and units for other WAHA engine paths remain unverified.

After separate authorization to activate and test v1.10.2, first query
selection replay from the consumer's last durable sequence for the same
conversation. If the original vote was durably saved, continue the same pending
request and question from that event, preserving its event ID, sequence and
original vote time, and check for duplicate effects. Do not reset or replace
the consumer request as a recovery shortcut. If no recoverable original vote
exists, use a fresh genuine tap on the still-supported poll or a supported
genuine text/voice answer; never fabricate a vote. A separate fresh poll may be
used as an optional smoke test. No live check has been performed as part of
this change.

### Copyable consumer handoff

```text
The poll service schema and consumer API are unchanged. A settled genuine vote
preserves its original vote time; a durably saved selection can be recovered
with the same event_id and selection_sequence through get_poll_selections.
Resume the same pending request/question and deduplicate by event identity and
sequence; do not create a replacement request to recover it. Integration
v1.10.2 is ready for consumer source-compatibility review; activation and live
WhatsApp proof remain pending separate authorization.
```
