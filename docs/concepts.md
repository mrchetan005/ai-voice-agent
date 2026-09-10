# Concepts

The mental model behind `voiceagent`: one agent definition, many channels, a
homogeneous worker fleet on self-hosted LiveKit.

## The agent

A [`VoiceAgent`](../src/voiceagent/agent.py) is sugar over a canonical
[`AgentConfig`](configuration.md): a name, a mode, a prompt, providers, tools,
memory, and turn-detection settings. `VoiceAgent(...)` takes flat keyword
arguments for the common case; `VoiceAgent.from_config(...)` loads a config
object, dict, or YAML path. Either way `.config` is the single source of truth,
so nothing behaves differently between the code-first and config-first paths.

## Pipeline vs realtime

Two modes, one shape:

- **pipeline** — STT → LLM → TTS as separate providers (e.g. Deepgram → Gemini
  → Cartesia). Turn detection via VAD or the multilingual turn-detector model.
- **realtime** — a single speech-to-speech model (Gemini Live, OpenAI Realtime)
  fills the LLM slot; no separate STT/TTS.

Switching modes is a config change. Both compile down to a livekit-agents
`AgentSession`; see [`livekit/compile.py`](../src/voiceagent/livekit/compile.py).

## Providers and the registry

Providers are named factories in a [registry](providers.md): `llm`, `stt`,
`tts`, `realtime`, and `vad` kinds. Built-ins (google, openai, deepgram,
cartesia, elevenlabs, silero, mock) register lazily so the core installs without
the `agents` extra. Config references a provider by name (`llm: google`), the
compile seam turns the `ProviderSpec` into a real plugin instance, and the
worker refuses to serve an agent whose provider keys are missing (loudly, at
startup). Add your own with [register_llm/stt/tts](adding-a-provider.md).

## Three layers, one rule

```
voiceagent            core: config, prompts, tools, memory, events, registry
  │                   (ZERO livekit imports — enforced by a test)
  ├── voiceagent.livekit    the ONLY package importing livekit*: provider
  │                         plugins, the compile seam, the worker, recording,
  │                         the aiortc<->room audio bridge
  ├── voiceagent.platform   FastAPI control plane (sessions, tokens, SIP)
  └── voiceagent.channels   browser / SIP / WhatsApp adapters
```

The core stays engine-neutral: events, tools, and memory never see a livekit
type. `tests/unit/test_layering.py` fails the build if a `livekit` import leaks
outside `src/voiceagent/livekit/`.

## The worker fleet and dispatch

A worker registers **one** LiveKit `agent_name` (`VOICEAGENT_WORKER_NAME`,
default `voiceagent`) and serves **all** loaded agents. Which agent runs a given
session is decided by dispatch metadata: [`SessionMetadata`](../src/voiceagent/metadata.py)
travels in the room's agent-dispatch metadata, and its `agent_id` selects the
`VoiceAgent`. So the fleet is homogeneous — every pod is interchangeable and
scales as one Deployment. See [deploy-k8s](deploy-k8s.md).

Overload is handled by LiveKit, not guesswork: each worker's `load_threshold`
(default 0.7) makes a saturated worker self-mark unavailable, so sessions never
land on a full pod. CPU autoscaling only adds capacity behind that backpressure
([load-testing](load-testing.md) sizes the fleet with real numbers).

## The control plane

The [platform API](channels-browser.md) (`voiceagent-api`) is a bearer-auth
FastAPI app. `POST /v1/sessions` creates a room, mints a LiveKit join token with
the agent-dispatch metadata, records the session in Redis (live status) and
Postgres (durable row), and returns `{session_id, room, livekit_url, token}`.
`GET /v1/sessions/{id}` reports status; `GET /v1/agents` lists servable agents;
`POST /v1/calls/sip` places outbound calls. `/healthz`, `/readyz`, `/metrics`
are open; everything else needs the token.

## Channels

The same agent is reachable from multiple [channels](channels-browser.md):

- **browser** — the platform mints a token; the browser joins over WebRTC.
- **sip** — inbound trunks + dispatch rules, or outbound via `/v1/calls/sip`;
  media rides LiveKit SIP. See [channels-sip](channels-sip.md).
- **whatsapp** — a standalone `voiceagent-whatsapp` service bridges Meta's
  WebRTC leg into a room. See [channels-whatsapp](channels-whatsapp.md).

The agent never learns which channel it is on; `SessionMetadata.channel` carries
it for accounting only.

## Memory, events, cost

- [Memory](memory.md) is an ABC (`load/append/clear/aclose`) with inmemory,
  Redis (fail-open), and Postgres backends, keyed by `memory_key` (falling back
  to `user_id`, then `session_id`).
- [Events](../src/voiceagent/events.py) are frozen dataclasses (SessionStarted,
  UserTranscript, AgentReply, ToolCall*, UsageUpdated, …) carrying `SessionIDs`.
  A bridge translates livekit session events into these; your `on_event`
  handlers, the metrics recorder, and memory all fan out from one dispatcher.
- Usage is normalized per session, priced by a [cost table](observability.md)
  (`PRICE_<PROVIDER>_<UNIT>` env overrides), and written to the Postgres session
  row alongside the raw payload so cost is recomputable.

## Where to go next

[quickstart](quickstart.md) to run one · [configuration](configuration.md) for
every field · [deploy-local](deploy-local.md) then [deploy-k8s](deploy-k8s.md)
to ship it.
