# Bounded WhatsApp media transport

Media transport is an opt-in, authenticated byte-transfer layer for the
`waha_whatsapp` integration. It publishes opaque inbound attachment
references, retrieves one exact referenced message on demand, stages bounded
uploads, and sends staged bytes through an explicitly configured conversation.
It does not transcribe, synthesize, interpret, route, or act on message content.

This is the schema-v1 interface implemented by integration 1.8.0. Source tests
and synthetic fixtures are not a live Home Assistant or WhatsApp end-to-end
test. The broader event envelope is in [CHANNEL_API.md](CHANNEL_API.md).

Integration 1.8.1 adds the inbound voice-note EOF compatibility fix described
below. Public schemas and capabilities remain version 1.

## Enablement and requirements

The **Enable authenticated media transport** connection option is off by
default. It permits active Home Assistant administrators to use the media
actions for currently authorized direct, general-group, and guest
conversations. All active administrators have the same transport authority;
opaque IDs are scoped references, not per-person permission grants. A consumer
that serves users with narrower rights must enforce its own per-person access
policy before calling HA actions. Turning media off makes transfers
unavailable; it does not erase already staged or cached content.

Retrieval and sending require a loaded, working WAHA **GOWS 2026.9.1 or newer**
session. This conservative floor is checked on each operation. The repository
app already pins `devlikeapro/waha:gows-2026.9.1` to digest
`sha256:5a56c7610bd42336874efac3e2e43e51d7893f3274dcef73f27fa3120da134d6`
(app 0.2.4). Its existing configuration selects GOWS, stores WAHA files under
`/data/media` with `WHATSAPP_FILES_LIFETIME=86400`, and sets
`WHATSAPP_DOWNLOAD_MEDIA`, `WAHA_EVENTS_DOWNLOAD_MEDIA`, and
`WAHA_API_DOWNLOAD_MEDIA` from `download_media`, whose default is false. WAHA's
default media backend is local (see WAHA's pinned
[2026.9.1 `.env.example`](https://github.com/devlikeapro/waha/blob/2026.9.1/.env.example)).
App 0.2.4 also sets `WAHA_BASE_URL` to the same internal hostname used in
Supervisor discovery (`HOSTNAME` with underscores normalized to hyphens, port
3000). WAHA uses this setting to construct `/api/files/...` media URLs;
`WHATSAPP_API_HOSTNAME=0.0.0.0` is only the listener bind address. Separately
managed deployments must configure `WAHA_BASE_URL` to the exact reachable WAHA
API origin used by this integration.
The app option and environment mapping are visible in this repository's
[app config](../waha/config.yaml) and [runner](../waha/run.mjs). The integration
uses a per-message `downloadMedia=true` request rather than enabling
automatic downloads. WAHA 2026.8.2 added the needed behavior that this
parameter overrides disabled environment defaults and that an undownloaded
message still has media attributes but a null URL. Evidence: the
[2026.8.2 release](https://github.com/devlikeapro/waha/releases/tag/2026.8.2),
[2026.9.1 release](https://github.com/devlikeapro/waha/releases/tag/2026.9.1),
WAHA's [receive-media documentation](https://waha.devlike.pro/docs/how-to/receive-messages/),
and the [WAHA configuration reference](https://waha.devlike.pro/docs/how-to/config/)
which distinguishes media URL origin from the listener hostname.
WAHA lists `sendImage`, `sendFile`, and `sendVoice` as GOWS-capable APIs; the
2026.6.1 release says Plus features are part of the Core image, so this
contract does not depend on a Plus-only media endpoint. See the
[WAHA send API matrix](https://waha.devlike.pro/docs/how-to/send-messages/)
and [2026.6.1 release](https://github.com/devlikeapro/waha/releases/tag/2026.6.1).

The companion app keeps the pinned engine and false defaults without enabling
blanket download. Separately managed WAHA installations must be checked for the
same GOWS version/engine, matching media URL base, and false automatic event/API
download defaults. Do not enable blanket media download for this interface. When an
on-demand retrieval occurs, WAHA's local `/data/media` cache may retain that
copy for up to 86400 seconds (24 hours); this is separate from HA's one-hour
private cache described below. Keep WAHA API-key protection enabled for
`/api/files`; do not exempt the files route from WAHA API authentication.

## Authentication and binding

Every service action requires a call context identifying an active HA
administrator. The binary HTTP routes require a Home Assistant authenticated
request and active administrator. Use an HA bearer token or authenticated
browser session. Missing, inactive, or non-admin users are rejected. These
checks authorize HA administrators for all configured media routes; they do
not provide a separate ACL for individual household users and do not inherit a
consumer's current person, memory, or tool grants. A consumer must enforce
those grants before invoking HA. Service responses contain no bytes/base64,
local paths, WAHA URLs, or credentials.

Each reference binds to its config entry and exact opaque `conversation_id`;
inbound retrieval also binds the exact opaque `message_id`. Upload receipts
bind to the administrator who staged them and the selected conversation. Each
use checks current entry/session/account state and fresh route authorization.
Configured direct contacts, general groups, and current guest routes are not
interchangeable. Guest membership is reconciled and verified again at use
time. Stale, removed, disabled, cross-user, or mismatched references fail
closed. The integration fetches WAHA media only with its API key, from the
configured WAHA origin and `/api/files/` route, without following redirects.
It never fetches caller-supplied URLs or filesystem paths.

## Inbound event descriptor

When the media manager is active and an inbound `message` has
`hasMedia: true`, the existing `waha_whatsapp_event` message gains one safe
`attachments` descriptor. The event can contain normal message metadata or a
text caption, but not bytes, a provider URL, a local path, or a download result.

```json
{
  "schema_version": 1,
  "event_id": "evt_example",
  "type": "message.received",
  "config_entry_id": "entry_example",
  "conversation_id": "conv_example",
  "conversation_type": "direct",
  "occurred_at": "2026-10-17T10:00:00+00:00",
  "sender": {
    "contact_id": "contact_fixture",
    "notify_entity_id": "notify.whatsapp_fixture"
  },
  "message": {
    "id": "msg_example",
    "kind": "audio",
    "in_reply_to": null,
    "attachments": [
      {
        "attachment_id": "att_0123456789abcdef0123456789abcdef",
        "kind": "audio",
        "mime_type": "audio/ogg",
        "file_name": "voice.ogg",
        "size_bytes": 1800,
        "voice_note": true,
        "duration_ms": null,
        "retrievable": true
      }
    ]
  }
}
```

Descriptor fields are `attachment_id` (opaque, stable reference), `kind`
(`audio`, `image`, `file`, `unknown`), `mime_type`, `file_name`,
`size_bytes`, `voice_note` (`true`, `false`, `null`), `duration_ms`, and
`retrievable`. Unavailable facts are null. MIME and display name prefer the
normalized WAHA media metadata, with the nested GOWS message as a fallback;
size and duration use nested GOWS integer `fileLength` and `seconds` facts
(duration is seconds multiplied by 1000). Metadata is advisory; bytes are
checked independently during retrieval. `retrievable` means a reference
could be persisted for an authorized current route; every action still
rechecks authorization. A descriptor does not guarantee supported bytes.
GOWS `Info.MediaType` values `ptt` and `audio` map to `kind: audio`, `image`
maps to `image`, and `document` maps to `file`; when that discriminator is
absent/unknown, the raw nested message type and then MIME are fallback hints.

For GOWS, voice classification is true only when raw
`_data.Info.MediaType` is exactly lowercase `"ptt"`, false only when it is
exactly lowercase `"audio"`, and otherwise null. The integration does not
infer voice-note status from an Ogg/Opus container, duration, filename, MIME,
or a nested PTT field. This is intentionally conservative: forwarded or
re-uploaded voice audio may have lost the upstream PTT bit and arrive as
ordinary audio. WAHA's GOWS `Info.MediaType` uses the `ptt`/`audio`
discriminator in [whatsmeow MessageInfo](https://github.com/tulir/whatsmeow/blob/main/types/message.go)
and its [PTT/audio classifier](https://github.com/tulir/whatsmeow/blob/main/send.go).

In group messages, `conversation_id` remains the opaque group route and
`sender.participant_id` is a separate opaque participant identity. Group
quotes use the same-conversation opaque `message.in_reply_to` token. When GOWS
provides complete, roster-resolved context, `message.mentions` reports opaque
participant IDs; unavailable metadata stays explicitly unavailable. See the
synthetic `group_voice_note` event in the fixtures for all three fields.

Outbound observations may publish safe media facts (kind, MIME, size, hash,
filename, and explicit voice-send choice). They never publish staged upload
IDs, paths, bytes, URLs, or provider IDs. Redacted observations omit
attachment descriptors.

## Retrieve an inbound attachment

Call the response-only action using exact IDs from the event:

```yaml
action: waha_whatsapp.get_media
data:
  config_entry_id: entry_example
  conversation_id: conv_example
  message_id: msg_example
  attachment_id: att_0123456789abcdef0123456789abcdef
  max_bytes: 26214400 # optional, 1..25 MiB
response_variable: media_result
```

Success returns metadata and an authenticated relative download path, not
bytes:

```json
{
  "status": "ready",
  "config_entry_id": "entry_example",
  "conversation_id": "conv_example",
  "message_id": "msg_example",
  "attachment": {
    "kind": "audio",
    "mime_type": "audio/ogg",
    "file_name": "voice.ogg",
    "size_bytes": 1800,
    "voice_note": true,
    "duration_ms": null,
    "attachment_id": "att_0123456789abcdef0123456789abcdef",
    "sha256": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
  },
  "download": {
    "method": "GET",
    "path": "/api/waha_whatsapp/entry_example/conversations/conv_example/media/att_0123456789abcdef0123456789abcdef",
    "requires_ha_auth": true,
    "expires_at": "2026-10-17T11:00:00+00:00"
  }
}
```

The integration calls
`GET /api/{session}/chats/{chatId}/messages/{messageId}?downloadMedia=true`,
checks session, raw message/chat/sender and participant bindings, then streams
the returned same-origin `/api/files/...` content into bounded private HA
storage. It checks byte size and container; it does not read history or emit a
second event. A repeated request in the valid window reuses cached bytes.

Fetch `download.path` on the HA origin with the same HA admin authentication.
The route streams binary `200` with `Content-Type`, exact `Content-Length`,
`Content-Disposition: attachment`, `Cache-Control: no-store`, and
`X-Content-Type-Options: nosniff`. It writes 64 KiB chunks with a 60-second
deadline. Any `Range` header is rejected as `400 invalid_request`; ranges are
not supported. Copy/consume bytes into the consumer's own store before this
reference expires. Consumer copies are outside this integration's control and
will not be deleted by it.

## Stage outbound media

Upload one file with multipart form data to HA:

```http
POST /api/waha_whatsapp/{config_entry_id}/conversations/{conversation_id}/media
Authorization: Bearer <HA access token>
Content-Type: multipart/form-data; boundary=...

--...
Content-Disposition: form-data; name="file"; filename="report.pdf"
Content-Type: application/pdf

<binary bytes>
--...--
```

The only accepted multipart field is `file`; extra parts and transfer/content
encoding are rejected. A content type from the allowlist is required. The
maximum is 25 MiB. Bytes are content-sniffed, must match the declared type,
and are hashed; filename extensions are not trusted. No URL or path is
accepted. Upload staging has no idempotency key: if the HTTP response is lost
after the server stored the upload, that unreferenced object remains only until
its normal expiry; re-uploading creates a new receipt. The HTTP `201 Created`
receipt is metadata only:

```json
{
  "status": "ready",
  "config_entry_id": "entry_example",
  "conversation_id": "conv_example",
  "upload_id": "upload_0123456789abcdef0123456789abcdef",
  "kind": "file",
  "mime_type": "application/pdf",
  "file_name": "report.pdf",
  "size_bytes": 142,
  "sha256": "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
  "expires_at": "2026-10-17T11:00:00+00:00"
}
```

The receipt is bound to its uploader and exact conversation. A different HA
user, entry, or conversation cannot reuse it.

## Send a staged attachment

Use the response-only `send_media_to_conversation` action:

```yaml
action: waha_whatsapp.send_media_to_conversation
data:
  config_entry_id: entry_example
  conversation_id: conv_example
  upload_id: upload_0123456789abcdef0123456789abcdef
  kind: file # image, file, or audio; must match staged receipt
  as_voice_note: false
  client_request_id: consumer-request-0001 # required, 1..128 characters
  caption: "The report is attached." # optional, max 1024 characters
  reply_to_message_id: msg_example # optional opaque same-conversation token
  origin: application # automation, application, human, or unknown
  content_redacted: false
response_variable: send_result
```

Response:

```json
{
  "conversation_id": "conv_example",
  "client_request_id": "consumer-request-0001",
  "status": "accepted",
  "message_id": "msg_sent_example",
  "replayed": false
}
```

`message_id` is an opaque integration token or null if tracking was
unavailable. `status` is `pending`, `accepted`, `failed`, or `unknown`.
Failures/unknown results include `error_code`. Acceptance means the WAHA HTTP
request returned successfully, not delivery/read. A timeout, interruption,
malformed response, or unexpected post-submit error can mean the send occurred;
do not make a new submission until reconciling that external effect.

The durable identity is `(config_entry_id, conversation_id,
client_request_id)` in the same idempotency ledger as text sends. The ledger
persists content binding and intent before network I/O. Identical concurrent
calls return `pending`; identical settled calls return the saved result with
`replayed: true` and do not send twice. Reusing a key with different content
returns `request_conflict`. Pending/unknown requests restored after restart
remain fenced as unknown and are never auto-retried. Up to eight identical
restaged upload aliases may be associated with one canonical request; the
ninth returns `busy`. Settled keys are kept seven days; unresolved records
remain fences. A matching settled request can replay its saved result after
staging bytes or receipt metadata have expired; this does not transmit again.
The upload ID must be a persisted alias for the same administrator and
conversation. Never-known IDs still fail. The ledger is capped at 2048 records
and 1 MiB and shares its capacity with correlated text sends. After a settled
key expires or its storage is lost, deduplication continuity is no longer
guaranteed; this is not an exactly-once delivery system. Persistence/capacity
failure blocks a new submission.

Image and file uploads use WAHA `sendImage` and `sendFile`; bytes are embedded
in a credentialed server-to-server request, with no public file URL. Image
and file captions are supported. Ordinary audio uses `sendFile`. A native
voice send requires verified Ogg/Opus and `as_voice_note: true`; it uses
`sendVoice` with `audio/ogg; codecs=opus` and `convert: false`, so this layer
does no audio conversion or synthesis. Native voice captions are rejected. An
optional quote must resolve from the same conversation and is passed as
WAHA's `reply_to`. WAHA documents these endpoints on GOWS and the Ogg/Opus
voice format in its [send-message reference](https://waha.devlike.pro/docs/how-to/send-messages/).

## Capabilities, storage, and limits

`waha_whatsapp.get_channel_capabilities` reports active connection capability.
`media_retrieval` and `media_send` are top-level booleans. The
`media_contract` reports the effective limits: `version: 1`, `inbound_kinds`
and `outbound_kinds` (`audio`, `image`, `file`), `voice_note_detection`,
`voice_note_send`, `max_download_bytes` and `max_upload_bytes` (25 MiB each),
`cache_max_bytes` (256 MiB), `cache_max_files` (128),
`cache_retention_seconds` and `reference_retention_seconds` (3600),
`reference_max_items` (2048), and `mime_types`. It also reports
`caption_max_length` (1024 Unicode characters), `caption_kinds` (`image`,
`file`, ordinary `audio`), `voice_note_captions: false`,
`voice_note_mime_type: "audio/ogg; codecs=opus"`,
`voice_note_conversion: false`, and `voice_note_channels: [1, 2]`.
Operational limits are `backend_fetch_concurrency_per_entry: 1`,
`upload_concurrency_per_entry: 1`, `transfer_queue_per_entry: 4`,
`send_concurrency_global: 1`, `send_queue_global: 4`,
`connection_timeout_seconds: 10`, and `transfer_timeout_seconds: 60`.
Send capability additionally requires durable correlated-send storage. If
media is disabled/unavailable, top-level capabilities are false; when a
manager exists its nested inbound/outbound kind lists are empty for unavailable
directions, `caption_kinds` is empty when sending is unavailable, and its voice
flags are false.

Example while the enabled backend is ready (the synthetic contract fixture
also shows the effective direction gates when a manager exists but media is
disabled or unavailable; without a manager, only the top-level false flags are
reported):

```json
{
  "media_retrieval": true,
  "media_send": true,
  "media_contract": {
    "version": 1,
    "inbound_kinds": ["audio", "image", "file"],
    "outbound_kinds": ["audio", "image", "file"],
    "voice_note_detection": true,
    "voice_note_send": true,
    "max_download_bytes": 26214400,
    "max_upload_bytes": 26214400,
    "cache_max_bytes": 268435456,
    "cache_max_files": 128,
    "cache_retention_seconds": 3600,
    "reference_retention_seconds": 3600,
    "reference_max_items": 2048,
    "mime_types": [
      "audio/ogg", "audio/mpeg", "audio/mp4", "audio/wav", "audio/flac",
      "image/jpeg", "image/png", "image/gif", "image/webp", "application/pdf"
    ],
    "caption_max_length": 1024,
    "caption_kinds": ["image", "file", "audio"],
    "voice_note_captions": false,
    "voice_note_mime_type": "audio/ogg; codecs=opus",
    "voice_note_conversion": false,
    "voice_note_channels": [1, 2],
    "backend_fetch_concurrency_per_entry": 1,
    "upload_concurrency_per_entry": 1,
    "transfer_queue_per_entry": 4,
    "send_concurrency_global": 1,
    "send_queue_global": 4,
    "connection_timeout_seconds": 10,
    "transfer_timeout_seconds": 60
  }
}
```

Recognized signatures/headers are Ogg Vorbis/Opus, MP3, M4A/M4B, WAV, FLAC,
JPEG, PNG, GIF, WebP, and PDF. `file` documents are PDF only. Native voice is
validated Ogg/Opus only, including Ogg framing/CRC and the required Opus
identification header. Other accepted types use bounded signature/header
recognition; this is not full semantic validation of every format. Unsupported
signatures and declared/sniffed MIME mismatches are rejected. MIME allowlist:
`audio/ogg`, `audio/mpeg`, `audio/mp4`, `audio/wav`, `audio/flac`,
`image/jpeg`, `image/png`, `image/gif`, `image/webp`, `application/pdf`.

### Inbound voice notes without an EOS page

Some GOWS PTT recordings end after a complete Ogg page and complete audio
packet without setting the final end-of-stream (EOS) flag. Integration 1.8.0
rejected these as `invalid_media` before returning bytes to the consumer.
Integration 1.8.1 permits this case only on authenticated, exactly bound inbound
PTT retrieval when the recording is verified Ogg/Opus. This follows the
missing-EOS case anticipated by [RFC 7845 §3](https://www.rfc-editor.org/rfc/rfc7845.html#section-3).

Every framing, lacing, CRC, stream identity, sequence, continuation, codec
header and resource check still applies. Partial pages/packets, invalid
headers, empty streams, chaining and data after an explicit EOS remain errors.
The file is neither rewritten nor remuxed: original size and SHA-256 are
preserved. A clean packet boundary does not prove a complete spoken sentence.

The internal policy remains strict by default. Ordinary audio, unknown PTT
classification, uploads and outbound native voice still require EOS. No action,
multipart field or connection option can enable the compatibility policy.
App 0.2.4, GOWS engine requirements and automatic-download settings are unchanged.
See the [fix validation and update steps](VOICE_EOF_FIX.md).

Files are under HA's private `.storage/waha_whatsapp_media` directory
(directory mode `0700`, files `0600`). The global budgets are 25 MiB per
object, 256 MiB cached bytes, 128 cached files, and 2048 references. Transfer
chunks are at most 64 KiB and requests have a 60-second bound. Retrieval and
staging each use one active operation plus at most four waiting per entry.
Media send preparation is serialized integration-wide with one active send
and four waiting, which also imposes a one-active/four-waiting ceiling on an
individual entry. The cache does not evict unexpired content to free space;
full capacity returns a retryable storage/busy error.
The exact-message WAHA JSON response is capped at 1 MiB; the WAHA file stream
is capped at 25 MiB. Outbound media is capped at 25 MiB before Base64
preparation, and its WAHA response is capped at 64 KiB.

References and staged/cached bytes expire one hour after observation/staging;
reads do not renew the fixed TTL. Bytes are removed at expiry, while a
metadata tombstone may remain for one additional hour so a known stale ID
returns `media_expired`. Afterwards it returns `media_not_found`. Startup
restores the private index and still-valid bytes, deletes orphans and cleans
expired content. Unload/reload cancels active retrieval; it does not make an
outbound pending send safe to retry. Removing the entry removes that entry's
owned media state after active operations stop. Backups/storage loss can
affect cached bytes. Consumers should copy needed content to their own
retention store promptly.

## Disable and rollback

Set **Enable authenticated media transport** to off to block new actions. This
does not purge existing cache bytes or the durable correlated-send ledger.
Before a source rollback, disable media and retain a cold HA backup of
`.storage/waha_whatsapp_media`, the HA storage entry keyed as
`waha_whatsapp.correlated.<config_entry_id>`, the channel and guest state keyed
as `waha_whatsapp.channel.<config_entry_id>` and
`waha_whatsapp.guest.<config_entry_id>`, and the WAHA app's `/data` volume. A
rollback to source without media APIs removes the media services; it must not
treated as making a media send retryable. Keep durable pending/unknown send
fences intact. Restore and confirm those storage files before resuming sends;
do not delete them as part of rollback. On the app side, use the full cold WAHA
backup if rolling back engine state. No live app/configuration or runtime
rollback was performed during source work.

## Contract differences and narrower limits

| Area | Implemented behavior | Consumer consequence |
| --- | --- | --- |
| Naming and paths | `get_media`, `send_media_to_conversation`, and the routes shown above | Names match the agreed consumer contract. |
| WAHA support floor | GOWS 2026.9.1 or newer | Stricter than the minimum needed for the 2026.8.2 on-demand override; older engines report unsupported. |
| Send queue | One active send and four waiting integration-wide | One entry is also bounded by this shared ceiling. |
| Re-staged upload aliases | At most eight aliases for one canonical idempotency request | The ninth identical restage is rejected as busy; no extra send is made. |
| Documents | PDF only for `kind: file` | Other document formats are rejected. |
| Voice send | Verified Ogg/Opus only; `convert: false`; no native voice caption | The layer does not transcode or synthesize audio. |
| Authorization | Active HA administrators only, with route revalidation | Consumers must enforce any narrower per-person policy independently. |

## Errors

`get_media` and upload route failures use
`{"status":"failed","error":{"code":"...","retryable":false}}`.
The response's `retryable` boolean is authoritative: it is true for transient
queue, backend, transport, storage, or timeout failures when retrying that
transfer can help, and false for input, authorization, route, expiry, disabled,
size, and unsupported-media failures. Staging is not idempotent, so an upload
timeout can leave an unreachable object that expires normally.

| HTTP status | Safe code or source | Meaning |
| --- | --- | --- |
| 400 | `invalid_request` | Invalid multipart shape or unsupported `Range` header. |
| 401 | HA authentication middleware | Missing/invalid authentication is rejected before the media view; body format is HA-owned. |
| 403 | `not_authorized` | User is inactive or not an HA administrator. |
| 404 | `media_not_found`, `invalid_conversation` | Reference or configured route is absent/mismatched. |
| 410 | `media_expired` | Known reference expired. |
| 413 | `media_too_large` | Request or transfer exceeds 25 MiB. |
| 415 | `unsupported_media_type` | MIME/signature is outside the allowlist. |
| 422 | `invalid_media`, `unsupported` | Bytes fail validation or cannot be used for the requested transport. |
| 429 | `busy`, `rate_limited` | Bounded queue/backend limit; includes `Retry-After: 5`. |
| 502 | `transport_failure`, backend `invalid_media` | Provider failed or returned an unsafe/invalid response. |
| 503 | `media_disabled`, `channel_unavailable`, `storage_unavailable` | Feature, channel, or private storage is unavailable. |
| 504 | `transport_failure` | Upload or download deadline expired. |

`request_conflict`, `invalid_reply_target`, `persistence_unavailable`, and
`outcome_unknown` are service-level send codes, not media-route HTTP statuses.
Send results have `status`, `message_id`, `error_code` when present, and
`replayed`; they have no `retryable` property. The durable statuses are
`pending`, `accepted`, `failed`, and `unknown`. Exact request-key replay returns
the stored outcome without another transmission, including a saved failure.
An `unknown` outcome is never automatically retried; reconcile whether it was
sent before choosing any new request ID. Other send error codes do not imply
that reusing the key resubmits: an identical key only replays its ledger entry.
HTTP error bodies do not include provider content or credentials. A download
can fail after headers begin; the connection closes without a replacement JSON
body. Verify `Content-Length` and SHA-256 returned by `get_media`.

## Contract fixtures

[`examples/media-v1.json`](examples/media-v1.json) contains synthetic inbound
voice/audio/image/document/unknown events and get/upload/send success, replay,
expiry, conflict, unsupported and disabled examples.
[`examples/media-fixtures/`](examples/media-fixtures/) contains small
locally-generated Ogg/Opus, MP3, PNG and PDF byte fixtures plus SHA-256 hashes.
They contain no personal media and are not captures from a real WhatsApp
account. Verify the exact checked-in bytes by running
`cd docs/examples/media-fixtures && shasum -a 256 -c SHA256SUMS`.
