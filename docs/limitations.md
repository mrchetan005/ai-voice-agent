# Limitations & deferred work

What this framework does **not** do yet, and what has been built but not yet
verified against a live third party. Nothing here is a silent gap — each item is
a deliberate scope boundary with a known path forward.

## Deferred verification (built, not yet live-tested)

These paths are implemented and tested offline against recorded fixtures /
mock providers, but a final check against the real third party is pending
(third-party API credits are limited, so live runs are deliberate and frugal):

| Area | Status | What's left |
|------|--------|-------------|
| Real providers (Deepgram/Gemini/Cartesia, Gemini Live) | offline path proven end-to-end on mock; usage→cost→Postgres verified with a mock session | one frugal `pytest -m live` run per provider |
| SIP inbound | trunk + dispatch rule created against local LiveKit; container boots and connects | a softphone (Zoiper/linphone) inbound call |
| WhatsApp Calling | webhook verify, SDP munge, and the aiortc↔room pacer are unit-tested; the SDP munge is a verbatim production port | one live Meta call — the only untested delta is media destination (room vs engine) |

## Known limitations (by design, for now)

- **Provider preflight isn't extensible.** The worker refuses to serve an agent
  whose built-in provider keys are missing, but that key check
  (`missing_provider_keys`) only knows the built-in providers. A custom provider
  registered via [adding-a-provider](adding-a-provider.md) gets no startup key
  preflight — validate its credentials yourself.
- **Built-ins re-register on every compile.** `ensure_builtins(overwrite=True)`
  runs at each session build, so a custom provider that reuses a built-in name
  (e.g. your own `openai` llm) is overwritten. Use a distinct name.
- **VAD is not configurable from `AgentConfig`.** `turn_detection` selects VAD
  vs the multilingual turn-detector, but the Silero VAD itself is loaded with
  defaults. A custom VAD enters only via the registry or the `build_session`
  seam.
- **Single homogeneous fleet.** One worker `agent_name` serves all loaded
  agents; run isolated fleets by giving them different `VOICEAGENT_WORKER_NAME`
  values and separate Deployments. See [concepts](concepts.md).
- **Multi-tenancy is plumbed, not enforced.** `tenant_id` flows through
  metadata, events, and the session row, but there is no per-tenant authn/z —
  the platform is single bearer-token. Front it with your own gateway.
- **Autoscaling is CPU + load-threshold, not queue-depth.** The real overload
  guard is each worker's `load_threshold` (a saturated worker self-marks
  unavailable); CPU HPA adds capacity behind it. A prometheus-adapter custom
  metric on `lk_agents_*` worker load is the documented upgrade path
  ([load-testing](load-testing.md)).

## Not implemented

- Vector / semantic memory (memory is last-N message history per key).
- Video (audio-only across all channels).
- DTMF / IVR menu flows over SIP.
- Dynamic pricing feeds (the [cost table](observability.md) is static with
  `PRICE_<PROVIDER>_<UNIT>` env overrides).
- Meta WhatsApp SIP-mode ↔ livekit-sip interop (the bridge uses aiortc, not
  LiveKit SIP).

## Platform notes

- **Windows dev:** the canonical dev loop runs the worker in Docker (Linux,
  matches prod). Native Windows is fine for `console` quick tests. Docker
  Desktop on Windows can't bind-mount the `E:` drive, which is why the compose
  stack ships configs inline over the Docker API rather than via bind mounts.
