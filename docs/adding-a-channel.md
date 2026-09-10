# Adding a channel

A channel adapter does three things: create/name a LiveKit room, dispatch an agent into it with `SessionMetadata`, and bridge the caller's media into that room — the agent runs unchanged whether it's browser, SIP, or WhatsApp.

The worker fleet is homogeneous: one fleet (registered under `VOICEAGENT_WORKER_NAME`)
serves every agent and every channel. The agent never learns which channel it's on; the
only channel-specific code lives in the adapter. Build on the thin wrappers in
`voiceagent.livekit.api` so the adapter stays engine-neutral (core `voiceagent` and most
channel code never import `livekit*` directly — a test enforces the layering).

## The three responsibilities

### 1. A room

Just a name. Existing adapters namespace by channel:
`va-{agent_id}-{id}` (browser), `sip-out-{id}` (outbound SIP), `wa-{id}` (WhatsApp).
LiveKit auto-creates rooms on join/dispatch.

### 2. Dispatch an agent with metadata

Attach a `SessionMetadata` JSON blob to the dispatch; the worker parses it in its job
entrypoint. Parsing is tolerant — a bad field degrades to its default rather than failing
the session. Fields (`voiceagent.metadata.SessionMetadata`):

| Field | Default | Notes |
| --- | --- | --- |
| `v` | `1` | schema version |
| `tenant_id` | `"default"` | |
| `agent_id` | `""` | **selects which loaded `VoiceAgent` runs** |
| `session_id` | `""` | |
| `channel` | `"browser"` | free string; existing: `browser` \| `sip` \| `whatsapp` \| `test` |
| `user_id` | `None` | |
| `call_id` | `None` | |
| `prompt_vars` | `{}` | `dict[str,str]` merged into the prompt |
| `memory_key` | `None` | conversation-memory key |
| `record` | `False` | start recording |
| `language` | `None` | whitelisted runtime override |

There are three dispatch mechanisms — pick by how media arrives:

**a. Token-carried dispatch** — the caller is its own WebRTC peer (browser). Mint a join
token with `worker_name` set; the token embeds a `RoomAgentDispatch`, so the worker joins
as soon as the participant connects. No server-side media handling.

```python
from voiceagent.livekit.api import mint_join_token
token = mint_join_token(
    settings.livekit, room=room, identity=user_id or "user-x",
    worker_name=settings.agent_source.worker_name, metadata=meta,
)  # hand the token to the client
```

**b. Server-side dispatch** — you own the media leg and push the worker in yourself
(outbound SIP, WhatsApp).

```python
from voiceagent.livekit.api import dispatch_agent
await dispatch_agent(settings.livekit, room=room,
                     worker_name=settings.agent_source.worker_name, metadata=meta)
```

**c. Dispatch rule** — provider-triggered inbound (inbound SIP). A one-time rule
(`create_inbound_dispatch_rule`) drops each caller into a room and dispatches the fleet;
the worker fills `session_id`/room from the job, so the rule's metadata only needs
`agent_id` + `channel`.

### 3. Bridge media into the room

- **Client is the peer** (browser): nothing to do — it publishes/subscribes directly.
- **SIP**: LiveKit's SIP service is the media bridge. Dial with `create_sip_outbound`
  (drops the callee into the room) or accept via the inbound trunk + rule.
- **External WebRTC** (WhatsApp/aiortc): reuse `RoomCallBridge`
  (`voiceagent.livekit.room_audio`) — the one place aiortc and `livekit.rtc` meet. It
  publishes a mic track into the room, pumps room audio out through a paced
  `OutboundAudioTrack`, and honors barge-in via the `va.control` / `b"clear"` data-topic
  protocol. It produces raw SDP; keep any provider-specific SDP quirks in your channel
  layer (as WhatsApp does with `munge_sdp_for_meta`).

## `voiceagent.livekit.api` toolbox

| Function | Use |
| --- | --- |
| `mint_join_token(...)` | join JWT; with `worker_name` it carries the agent dispatch |
| `dispatch_agent(...)` | server-side dispatch into a room |
| `create_inbound_trunk(...)` | inbound SIP trunk for a set of DIDs |
| `create_inbound_dispatch_rule(...)` | route inbound trunk calls into per-caller rooms |
| `create_sip_outbound(...)` | dial a number and drop the callee into a room |
| `delete_room(...)` | tear the room down on hang-up |

## Where the adapter lives

- **In-process route** on the platform API (like browser sessions and outbound SIP):
  add a router under `voiceagent.platform.routes` and include it in
  `voiceagent.platform.app` (behind `require_token`). Best when there's no long-lived
  media leg to own.
- **Standalone service** (like WhatsApp): a separate process/console script when the
  channel owns its own media lifecycle and should scale independently. Register it in
  `[project.scripts]`.

Either way: name a room, dispatch with `SessionMetadata`, bridge media, and
`delete_room` on end. The agent side needs no changes.

## See also

- [concepts](concepts.md) · [configuration](configuration.md) · [providers](providers.md)
- [channels-browser](channels-browser.md) · [channels-sip](channels-sip.md) · [channels-whatsapp](channels-whatsapp.md)
