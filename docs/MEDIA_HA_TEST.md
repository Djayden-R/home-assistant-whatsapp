# Media transport live verification checklist

This short checklist is for the HA session owner after source/release review.
No production HA runtime, app, connection options, or WhatsApp account was
changed for this implementation. Source tests and synthetic fixtures do not
constitute a live WhatsApp end-to-end test.

1. Confirm app 0.2.4 (or an external server with `WAHA_BASE_URL` matching
   the integration origin), and a working GOWS 2026.9.1 or newer session. Keep the
   connection option **Enable authenticated media transport** off until a
   controlled test window. Keep the app's `download_media` option false and
   verify event/API auto-download defaults remain false.
2. During the approved window, enable media in HA and reload the entry if HA
   requires it. As an HA administrator, call
   `waha_whatsapp.get_channel_capabilities`; check `media_retrieval` and
   `media_send` are true. Confirm no automatic WAHA download option was
   enabled.
3. From a controlled test account/contact send one voice note, then return a
   normal same-chat text reply through the existing reviewed HA egress path.
   Check that the consumer receives the
   attachment reference, gets the binary with HA admin auth, copies it to its
   own retention store, and runs transcription only in the external local
   consumer. Record voice-message arrival-to-transcript latency and the
   consumer/transcriber peak RAM if that is part of the handoff acceptance;
   the integration itself performs no transcription. Then send ordinary audio,
   an image, and a PDF. Inspect only the safe event descriptor. Expect raw GOWS
   `Info.MediaType: "ptt"` to classify as voice; `"audio"` is ordinary audio.
   Confirm events include no bytes/base64, provider URL, local path, or raw
   WAHA ID.
4. Call `waha_whatsapp.get_media` with exact conversation/message/attachment
   IDs. Download the returned relative path using HA admin authentication.
   Verify size, SHA-256 and MIME, and that a Range request is rejected. Check
   stale routes and non-admin users cannot retrieve bytes.
5. Stage an image, PDF, ordinary audio, and synthetic Ogg/Opus using the
   authenticated multipart route. Use stable request IDs to send each to the
   controlled conversation. Verify attachments and captions, and native PTT
   for verified Ogg/Opus with `as_voice_note: true`. Check replay does not
   duplicate a send and changed content under the same key conflicts. Never
   automatically retry an `unknown` result; reconcile it externally first.
6. Turn the media option off and reload the entry. Verify capabilities become
   false and transfers fail closed. Keep consumer copies only under the
   consumer's intended retention policy; remove controlled test messages
   through normal account procedures if desired.
