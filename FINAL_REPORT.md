# Final report — `voiceagent` framework

Greenfield rebuild on branch `feature/livekit-scalable-voice-platform`: a reusable,
production-grade voice-agent framework around **self-hosted LiveKit** and
**Kubernetes**. `main` is untouched. This report is the M9 deliverable — a single
pass over what was built, how it was verified, and what remains.

## 1. What it is

Define an agent once and run the *same* agent across browser WebRTC, SIP/PSTN, and
WhatsApp Calling, then scale it on Kubernetes:

```python
from voiceagent import VoiceAgent, run

agent = VoiceAgent(
    name="sales-agent",
    llm="google", model="gemini-2.5-flash",
    stt="deepgram", tts="cartesia",
    system_prompt="You are a concise sales assistant for {company}.",
    language="en",
)
run(agent)   # dev / start / console
```

Pipeline (STT→LLM→TTS) and realtime (Gemini Live / OpenAI Realtime) are the same
API with a `mode=` switch — a config change, not an architecture change.

## 2. Engine decision

`livekit-agents ~=1.8.0` is the session engine. We do **not** rebuild what LiveKit
provides — AgentSession (pipeline + realtime behind one interface), worker
dispatch/drain/load, and the Deepgram/Cartesia/ElevenLabs/Google/OpenAI/Silero/
turn-detector plugins. Self-hosted rule held throughout: always plugin classes,
never Cloud-only string model ids.

## 3. Architecture — three layers, one rule

```
voiceagent (core: config, prompts, tools, memory, events, registry — ZERO livekit imports)
   └── voiceagent.livekit (THE ONLY place importing livekit*: providers, compile, bridge, worker, recording)
         ├── voiceagent.platform (FastAPI control plane: sessions/agents/sip/health)
         └── voiceagent.channels (browser = platform routes; sip helpers; whatsapp standalone bridge)
```

The layering rule is enforced by an AST test (`tests/unit/test_layering.py`): a
`livekit*` import anywhere outside `src/voiceagent/livekit/**` fails the suite. The
core package imports cleanly with no extras installed (`run()` lazily imports the
livekit layer).

## 4. Milestones — all complete

| # | Milestone | Verification |
|---|-----------|-------------|
| M0 | Clean slate: branch, delete old trees, new pyproject/skeleton/settings | `import voiceagent`, ruff, pytest green |
| M1 | Core API: config/prompts/tools/memory/events/registry/metadata | unit tests + no-livekit-import lint |
| M2 | LiveKit layer: providers+mocks, compile, bridge, worker, `run()` | unit tests; `console` speaks offline |
| M3 | Compose + platform + browser E2E (spec §29 gate) | browser demo w/ mock agent; `-m integration` |
| M4 | Real providers + memory backends + usage→cost→Postgres | offline proven; frugal `-m live` **user-gated** |
| M5 | Observability + recording: OTel, Prometheus, MinIO egress | metrics on `/metrics`; OGG to MinIO |
| M6 | SIP: setup helper, outbound endpoint, compose profile | offline; softphone inbound **user-gated** |
| M7 | WhatsApp service: ported client/webhook/SDP + bridge | webhook/SDP/pacing unit tests; live Meta **deferred** |
| M8 | K8s chart + values + sip manifests + loadtest | `helm lint`/`template` clean; kubeconform valid |
| M9 | Docs + README + limitations + final report + validation sweep | this document |

Each milestone was one conventional commit, authored **Chetan only** (no
Co-Authored-By trailer), pushed immediately.

## 5. Public API

`VoiceAgent`, `run`, `AgentConfig` (+ `LLMConfig/STTConfig/TTSConfig/RealtimeConfig/
MemoryConfig`), `PromptTemplate`, `PromptConfig`, `tool`, `Tool`, `ToolContext`,
`ToolFailure`, `Memory`, `Message`, `SessionMetadata`, `SessionIDs`, the event
classes, and `registry` + `register_llm/stt/tts/realtime`. `VoiceAgent.from_config`
accepts an `AgentConfig`, dict, or YAML path. Full reference in
[docs/configuration.md](docs/configuration.md), [docs/prompts.md](docs/prompts.md),
[docs/tools.md](docs/tools.md), [docs/memory.md](docs/memory.md).

## 6. Providers

Built-ins registered lazily by `ensure_builtins()`: llm `google|openai|mock`, stt
`deepgram|mock`, tts `cartesia|elevenlabs|mock`, realtime `google|openai`, vad
`silero`. The mock triplet is what makes the whole offline suite (and the browser
demo) run with **no keys and no network**. Custom providers plug in via
`register_llm/stt/tts/realtime`. See [docs/providers.md](docs/providers.md) and
[docs/adding-a-provider.md](docs/adding-a-provider.md).

## 7. Channels

- **Browser** — platform mints a join token with `RoomAgentDispatch` metadata; the
  static `/demo` page joins the room, publishes mic, plays agent audio.
- **SIP/PSTN** — `python -m voiceagent.channels.sip setup` creates the inbound
  trunk + dispatch rule; outbound via `POST /v1/calls/sip`. livekit-sip runs via
  compose profile or a plain K8s Deployment. The worker has zero SIP awareness.
- **WhatsApp Calling** — a standalone `voiceagent-whatsapp` service: Meta SDP
  webhook → room + agent dispatch → aiortc↔room bridge with the production-proven
  20 ms pacer and SDP munge ported verbatim.

See [docs/channels-browser.md](docs/channels-browser.md),
[docs/channels-sip.md](docs/channels-sip.md),
[docs/channels-whatsapp.md](docs/channels-whatsapp.md),
[docs/adding-a-channel.md](docs/adding-a-channel.md).

## 8. Deployment

- **Local** — `deploy/compose/docker-compose.yml`: LiveKit v1.13.6 + Redis +
  Postgres + api + worker, inline configs (no host bind mounts — works on Windows
  Docker Desktop). Profiles `recording` (MinIO + Egress) and `sip`. One command to
  the zero-credit browser demo. [docs/deploy-local.md](docs/deploy-local.md).
- **Kubernetes** — upstream `livekit/livekit-server` + `livekit/egress` charts, our
  `charts/voiceagent` chart (api + worker Deployments, HPA, PDB, migration hook Job,
  agents ConfigMap), and plain SIP manifests. The in-cluster URL is the
  cross-namespace FQDN `ws://livekit-livekit-server.livekit.svc.cluster.local:7880`
  (the upstream chart names its Service `<release>-livekit-server`; a bare
  `ws://livekit:7880` will not resolve). [docs/deploy-k8s.md](docs/deploy-k8s.md),
  plus cloud deltas in [docs/deploy-gke.md](docs/deploy-gke.md) and
  [docs/deploy-eks.md](docs/deploy-eks.md).

Autoscaling: each worker's `load_threshold` (0.7) is the real overload guard — a
saturated worker self-marks unavailable and LiveKit stops dispatching to it. CPU
HPA (60%, below 0.7) only adds capacity behind that backpressure. Size
`maxReplicas` from measured sessions-per-worker via `deploy/loadtest`, not CPU
guesses.

## 9. Observability, recording, cost

OTel traces feed `livekit.agents.telemetry.set_tracer_provider`. Prometheus: the
framework's own metrics use the `va_*` prefix (`va_sessions_started_total`,
`va_session_duration_seconds`, `va_tool_calls_total`, `va_usage_units_total`,
`va_cost_usd_total`); the worker also exposes livekit-agents' `lk_agents_*` fleet
metrics. Cost is a static price table with `PRICE_<PROVIDER>_<UNIT>` env overrides;
raw usage is stored alongside the normalized form so cost is recomputable
retroactively. Recording is a fire-and-forget audio-only Egress (OGG → S3/MinIO)
with the URL written onto the session row. See
[docs/observability.md](docs/observability.md),
[docs/recording.md](docs/recording.md), [docs/load-testing.md](docs/load-testing.md).

## 10. Verification evidence (this sweep)

- `uv run ruff check src tests` → **All checks passed!**
- `uv run pytest -q` → **195 passed, 2 deselected** (live + integration), fully
  offline, no keys, no network.
- No-livekit-import guard: passes (part of the suite).
- M8 Helm: `helm lint` 0 failed; `helm template` renders 11 resources with the
  corrected FQDN; `kubeconform` chart 11/11 + sip 3/3 valid (docker/stdin, offline).
- Browser E2E (spec §29 gate): passed at M3 with the mock agent (zero credits).

## 11. Documentation set

20 pages under `docs/` (indexed from the README): quickstart, concepts,
configuration, prompts, tools, memory, providers, adding-a-provider, channels-
{browser,sip,whatsapp}, adding-a-channel, deploy-{local,k8s,gke,eks}, observability,
recording, load-testing, limitations.

## 12. Deferred / known limitations

Tracked in [docs/limitations.md](docs/limitations.md). Summary:

- **Deferred live verification** (built + tested offline, one frugal live check
  pending, gated on credentials): real providers (`-m live` per provider); SIP
  inbound (softphone call); WhatsApp Calling (one live Meta call — the only
  untested delta is media destination, room vs engine).
- **By design, for now:** provider preflight covers only built-ins; built-ins
  re-register each compile (use distinct custom names); VAD not configurable from
  `AgentConfig`; single homogeneous fleet per `agent_name`; multi-tenancy plumbed
  but not enforced (single bearer token — front it with your own gateway);
  autoscaling is CPU + load-threshold, not queue-depth.
- **Not implemented:** vector/semantic memory; video; DTMF/IVR; dynamic pricing
  feeds; Meta WhatsApp SIP-mode ↔ livekit-sip interop.

## 13. Next steps

1. One frugal `pytest -m live` per provider (keys from `.env`), confirm a session
   row shows usage + cost.
2. One softphone inbound SIP call against the local `sip` profile.
3. One live Meta WhatsApp call with metrics on (rollback is trivial — `main` keeps
   the working direct path).
4. `pytest -m integration` against the compose stack ($0).
5. Load-test run against the compose mock agent to produce the latency/capacity
   table and size K8s `maxReplicas`.
