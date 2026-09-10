# Browser channel (WebRTC)

Connect a browser to an agent by asking the platform API for a LiveKit token, then joining the room over WebRTC — the same agent fleet that serves SIP and WhatsApp.

The browser is the simplest channel: the client is its own media peer. The API mints a
join token that *carries the agent dispatch*, so the worker joins the moment the browser
connects — no server-side media bridge, no separate dispatch call.

## Flow

```
browser ──POST /v1/sessions──▶ platform API ──mint token (+dispatch)──▶ returns token
browser ──WebRTC connect(livekit_url, token)──▶ LiveKit room ◀── agent worker joins
```

## POST /v1/sessions

Bearer auth (`Authorization: Bearer $PLATFORM_API_TOKEN`) — like every `/v1/*` route.

Request (`CreateSessionIn`):

```jsonc
{
  "agent_id": "mock-demo",   // required; must be a loaded agent (404 otherwise)
  "channel": "browser",      // default "browser"
  "user_id": null,           // becomes the participant identity when set
  "prompt_vars": {},         // dict[str,str] merged into the system prompt
  "memory_key": null,        // conversation-memory key (see memory.md)
  "record": false,           // start Egress recording (see recording.md)
  "language": null           // whitelisted runtime language override
}
```

Response `201` (`CreateSessionOut`):

```json
{
  "session_id": "0f1e2d...",
  "room": "va-mock-demo-0f1e2d3c",
  "livekit_url": "ws://127.0.0.1:7880",
  "token": "eyJhbGciOi..."
}
```

- `room` is `va-{agent_id}-{session_id[:8]}`.
- `livekit_url` is LiveKit's `effective_public_url` (`LIVEKIT_PUBLIC_URL` if set, else
  `LIVEKIT_URL`) — the URL the *browser* dials, which can differ from the API's own.
- The participant identity is `user_id` when supplied, else `user-{session_id[:8]}`.
- The token embeds a `RoomAgentDispatch` for the worker fleet (`VOICEAGENT_WORKER_NAME`)
  with the session's `SessionMetadata`, and has a 3600 s TTL.

```bash
curl -sX POST http://localhost:8080/v1/sessions \
  -H "Authorization: Bearer $PLATFORM_API_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"agent_id":"mock-demo","prompt_vars":{"company":"Acme"}}'
```

## GET /v1/sessions/{session_id}

Returns the merged status (in-memory) and the durable store record — `session_id`,
`agent_id`, `channel`, `room`, `status`, timings, usage/cost when present. `404` if the
session is unknown.

## DELETE /v1/sessions/{session_id}

Ends the call: deletes the LiveKit room and marks the session `ending`. Returns `204`,
or `404` if the session is unknown or already ended.

## Joining from the browser

Using `livekit-client`, connect with the returned `token` + `livekit_url`, publish the
mic, and attach the agent's audio track:

```js
import { Room, RoomEvent, Track, createLocalAudioTrack }
  from "https://cdn.jsdelivr.net/npm/livekit-client@2/+esm";

const res = await fetch(`${API}/v1/sessions`, {
  method: "POST",
  headers: { "Content-Type": "application/json", Authorization: `Bearer ${TOKEN}` },
  body: JSON.stringify({ agent_id: "mock-demo" }),
});
const { token, livekit_url } = await res.json();

const room = new Room();
room.on(RoomEvent.TrackSubscribed, (track) => {
  if (track.kind === Track.Kind.Audio) track.attach();   // autoplays agent audio
});
await room.connect(livekit_url, token);
await room.localParticipant.publishTrack(await createLocalAudioTrack());
```

Two side channels the agent uses:

- **Transcriptions** arrive as text streams on the `lk.transcription` topic
  (`room.registerTextStreamHandler("lk.transcription", ...)`); the sender's identity tells
  you caller vs. agent.
- **Agent state** (`listening` / `thinking` / `speaking`) arrives via
  `RoomEvent.ParticipantAttributesChanged` under `attrs["lk.agent.state"]`.

## Try it: the bundled demo

The platform serves `examples/browser-demo/` at **`/demo`** when the directory exists
(`PLATFORM_DEMO_DIR`, default `examples/browser-demo`). With the local stack up
([deploy-local](deploy-local.md)), open <http://localhost:8080/demo> — the `mock-demo`
agent needs no provider keys. The page does exactly the flow above: create session, join,
publish mic, render transcripts.

## See also

- [quickstart](quickstart.md) · [configuration](configuration.md) · [concepts](concepts.md)
- [channels-sip](channels-sip.md) · [channels-whatsapp](channels-whatsapp.md) · [adding-a-channel](adding-a-channel.md)
