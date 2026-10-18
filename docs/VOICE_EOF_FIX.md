# Inbound voice-note EOF compatibility fix

Integration source 1.8.1 fixes a transport rejection in released 1.8.0. The
app remains at 0.2.4 and the digest-pinned engine remains GOWS 2026.9.1.
There are no new credentials, public fields, services, options or dependencies.

## Cause and bounded change

The reported recording passed exact-message retrieval, route bindings, MIME,
Ogg page framing, CRCs and packet completeness. The final EOS flag was absent.
The shared validator required that flag for inbound media as well as uploads,
so retrieval ended with `invalid_media` before the consumer received a file.

[RFC 7845 §3](https://www.rfc-editor.org/rfc/rfc7845.html#section-3) anticipates
streams lacking a final EOS page and prohibits decoding incomplete trailing
packets. This fix accepts the narrower case of complete pages and packets only.
The compatibility policy is private, strict by default, and selected solely for
authenticated, exactly bound incoming GOWS PTT that is verified Ogg/Opus.

Uploads, ordinary/unknown audio and outbound native voice keep the strict EOS
requirement. Framing, sequence, serial, continuation, CRC, codec-header, stream
and resource checks remain enforced. Nothing sets an EOS bit, remuxes, repairs
or transcribes the recording. A packet-complete EOF is not a claim that an
entire sentence was recorded.

The public [no-EOS fixture](examples/media-fixtures/synthetic-voice-note-no-eos.ogg)
is derived from the existing synthetic silence recording by clearing only its
last page's EOS flag and recomputing that page's CRC. It is 474 bytes, SHA-256
`8ef7b11eb64a1f0b136f3f0814869daa911e9071ec44254ac5ad4bf0c5ce2688`.
It contains no household recording, transcript or identifier. Its original
EOS counterpart and checksums are in the same fixture directory.

## Broader release review

Fresh independent **gpt-6-astra at high reasoning** reviews covered the
channel, groups and correlated sends in PR #3, media transport in PR #4,
and the voice-note fix in PR #5. They found two actionable issues:

- A delayed restaging replay could let a retired ledger overwrite newer send
  receipts. New alias writes now require the current, active writer and
  available persistence. Existing exact receipt replays remain read-only.
  A paused-preflight/replacement/second-restart regression verifies that both
  receipts survive and no duplicate backend submission occurs.
- Restarting with all media options disabled left private-cache expiry
  unscheduled. Storage initialization now restores cleanup independently of
  enabling transfers. Direct manager removal also loads shared state before
  deleting its own records; normal successful HA unload already took the
  safe initialization path. Lifecycle tests cover multi-entry preservation,
  actual timer expiry while disabled, and corrupt/unreadable/write-failed
  indexes without resetting state.

The fixes are commits `8e5098e` and `c9c1b60`. There were no minor findings
to defer. The app, engine, public schemas and media defaults remain unchanged.

## Validation and update

The regression suite constructs CRC-correct EOS/no-EOS and malformed streams,
exercises the real media manager through a fake authenticated backend, and
checks the authenticated binary route preserves bytes, length and SHA-256.
Structural failures and strict outbound/upload behavior remain covered.
Generated silence can also be decoded with an optional development-only
FFmpeg check; there is no decoder dependency in the integration.

Source validation on 2026-10-18:

- Full pytest with the existing Home Assistant dependency path: **658 passed,
  no skips**. The repository-only environment: **602 passed, 11 skipped**
  because those tests require Home Assistant.
- Node tests: **20 passed**. Ruff, compilation, version checks for `v1.8.1`
  and unchanged `waha-v0.2.4`, Node syntax and whitespace checks passed.
- After both broader-review findings were fixed, a fresh independent
  **Astra High** follow-up found no remaining findings and separately passed
  **189 tests** across correlated sends/storage, media core/lifecycle,
  services and routes.
- **FFmpeg 8.1.2** decoded generated no-EOS silence successfully; the public
  474-byte no-EOS fixture also decoded successfully. Neither check rewrote
  the transport bytes or used a household recording.

The authenticated synthetic HTTP case includes five pages and 35 complete
packets, matching the reported structure. Full HTTP/Node tests used local
test listeners; they made no connection to a live HA or WhatsApp session.

Once integration 1.8.1 is published, the owning HA session should:

1. Update **WAHA WhatsApp** through the supported HACS flow and perform its
   required reload/restart. This source task does neither on a live server.
2. Keep app 0.2.4 and `download_media: false`. Do not change the engine, enable
   all-chat downloads or modify the external transcriber for this fix.
3. Verify scoped `get_media` and the authenticated binary GET using a fresh
   observed voice note, including matching size/digest. References from an
   earlier failure may already have reached their fixed one-hour expiry.
4. Have the owner send one fresh WhatsApp voice note and receive a contextual
   reply through the existing consumer/egress path.

Source, decoder and synthetic HTTP checks are not a successful live phone
end-to-end test. The household's runtime integration, consumer and HA state
are unchanged by this source work. Prefer disabling media while preserving
private stores over a source rollback. Keep the same request IDs for pending
or unknown sends and never reset the ledger to force a retry. If returning to
1.8.0 through the supported update flow, preserve private stores and reconcile
outstanding sends first; that version restores both lifecycle defects above
and rejects the affected no-EOS notes again.
