# WhatsApp Calling channel

Take and place WhatsApp voice calls through Meta's Cloud API via a standalone `voiceagent-whatsapp` service that verifies webhooks, munges SDP for Meta, and bridges the WebRTC leg into a LiveKit room.

WhatsApp runs as **its own process** (own webhook URL, own media lifecycle) so it scales
independently of the worker fleet. It never runs an `AgentSession`: it dispatches an agent
into a room and bridges the Meta WebRTC leg to that room. The agent runs as an ordinary
room participant and never learns it's on WhatsApp.

> **Status:** live Meta verification is deferred. The SDP munge, webhook parsing, and the
> outbound-flow wiring are exercised offline (`tests/unit/test_whatsapp_*`); a real Meta
> Business number end-to-end test is still pending. There is no compose service for it —
> run the console script directly.

## Run it

Console script `voiceagent-whatsapp` (fails fast if credentials are missing):

```bash
voiceagent-whatsapp     # serves on WHATSAPP_PORT (default 8090)
```

Required env — see [configuration](configuration.md) for the full group:

| Variable | Purpose |
| --- | --- |
| `WHATSAPP_ACCESS_TOKEN` | Graph API bearer (**required**) |
| `WHATSAPP_PHONE_NUMBER_ID` | the number placing/receiving calls (**required**) |
| `WHATSAPP_VERIFY_TOKEN` | webhook GET-verify handshake (`hub.verify_token`) |
| `WHATSAPP_APP_SECRET` | validates `X-Hub-Signature-256` on POSTs (optional; empty = skip) |
| `WHATSAPP_GRAPH_VERSION` | Graph version, default `v24.0` |
| `WHATSAPP_PORT` | listen port, default `8090` |
| `WHATSAPP_DEFAULT_AGENT` | `agent_id` that serves **inbound** calls |

Startup requires only `WHATSAPP_ACCESS_TOKEN` + `WHATSAPP_PHONE_NUMBER_ID`. Inbound calls
also need `WHATSAPP_DEFAULT_AGENT` set to a loaded agent, or they are rejected.

## Endpoints

| Method + path | Auth | Purpose |
| --- | --- | --- |
| `GET /webhooks/whatsapp` | none | Meta verification handshake |
| `POST /webhooks/whatsapp` | signature | Meta events (calls / statuses / messages) |
| `POST /v1/whatsapp/calls` | bearer | place an outbound call |
| `GET /healthz` | none | liveness |

**Verify** replies with `hub.challenge` when `hub.mode=subscribe` and `hub.verify_token`
matches `WHATSAPP_VERIFY_TOKEN`, else `403`.

**Events** are rejected `401` unless `X-Hub-Signature-256` matches an HMAC-SHA256 of the
raw body under `WHATSAPP_APP_SECRET` (skipped when `app_secret` is empty — a dev
convenience, not a production posture). The handler answers `200` fast and routes async;
SDP offers land on an inbound queue that the service drains and answers.

## POST /v1/whatsapp/calls (outbound)

Bearer `Authorization: Bearer $PLATFORM_API_TOKEN`.

Request (`OutboundCallIn`):

```jsonc
{
  "agent_id": "mock-demo",       // required; 404 if not loaded
  "to_number": "+15551234567",   // callee
  "user_id": null,
  "prompt_vars": {},
  "request_permission": false,   // send a call-permission prompt and wait for Accept first
  "answer_timeout_s": 30.0       // how long to wait for the SDP answer
}
```

Response `201`:

```json
{ "session_id": "3c2b...", "room": "wa-3c2b1a09", "call_id": "wacid..." }
```

Errors: `404` unknown agent · `409` that number already has an open call ·
`403` call-permission denied.

```bash
curl -sX POST http://localhost:8090/v1/whatsapp/calls \
  -H "Authorization: Bearer $PLATFORM_API_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"agent_id":"mock-demo","to_number":"+15551234567","request_permission":true}'
```

## How a call is bridged

Both directions dispatch an agent into a `wa-{session_id[:8]}` room, then wire a
`RoomCallBridge` between Meta's WebRTC peer (aiortc) and the room:

- **Outbound:** (optional permission request →) create an SDP **offer** →
  `munge_sdp_for_meta` → `initiate_call` (Graph `action=connect`) → wait for the answer on
  the `calls` webhook → `set_answer`.
- **Inbound:** the caller's webhook carries an SDP **offer** → create an SDP **answer** →
  munge → `pre_accept_call` then `accept_call`.

`munge_sdp_for_meta` makes an aiortc SDP pass Meta's RFC 8866 validator: it keeps only the
`sha-256` `a=fingerprint` (Meta error 138008 rejects multiple), drops `a=extmap`, and pins
`a=ptime:20` for Opus. It is a pure string transform, tested offline.

Inside the bridge (`voiceagent.livekit.room_audio`): Meta audio (48 kHz Opus) is decoded
and captured into the room at 48 kHz mono (no resampling); the agent's room track is paced
into 20 ms frames by `OutboundAudioTrack` (buffer capped at ~240 ms — oldest audio dropped
on overflow so latency can't accumulate). Barge-in is honored: when the worker publishes
`b"clear"` on the `va.control` data topic, the outbound buffer is flushed.

Note the calling allowlist stores numbers in `wa_id` form **without** a leading `+`
(the client strips it); messaging normalizes both, calling does not.

## See also

- [configuration](configuration.md) (`WHATSAPP_*`) · [concepts](concepts.md) · [deploy-k8s](deploy-k8s.md)
- [channels-browser](channels-browser.md) · [channels-sip](channels-sip.md) · [adding-a-channel](adding-a-channel.md)
