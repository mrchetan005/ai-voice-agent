# Observability

OpenTelemetry traces for AgentSession spans, Prometheus metrics on two `/metrics` endpoints (`va_*` app metrics on the worker/platform, `lk_agents_*` runtime metrics on the worker), and per-session usage→cost accounting priced from a built-in rate table.

Everything here is optional. Install the extra to enable it:

```bash
uv pip install 'voiceagent[obs]'   # opentelemetry + prometheus_client
```

If `prometheus_client` is not installed, every metrics hook is a no-op. If `opentelemetry` is not installed, tracing logs a warning and stays off. Neither is ever load-bearing on the realtime path.

## Endpoints

| endpoint | port | serves |
|----------|------|--------|
| `/metrics` (worker) | `WORKER_PROMETHEUS_PORT` (9100) | `lk_agents_*` runtime metrics **and** the `va_*` session/tool/usage/cost metrics |
| `/metrics` (platform) | `PLATFORM_PORT` (8080) | `va_api_sessions_created_total` (control plane) |
| `/healthz` (platform) | 8080 | liveness — `{"status":"ok"}` |
| `/readyz` (platform) | 8080 | readiness — 200 when LiveKit is reachable, 503 otherwise; Redis is reported but fail-open |

```bash
curl -s localhost:9100/metrics | grep -E '^(va_|lk_agents_)'   # worker
curl -s localhost:8080/metrics                                  # platform
curl -s localhost:8080/readyz | jq
```

The worker endpoint is served by livekit-agents itself. The framework's own counters are incremented inside job subprocesses and aggregate onto that same port because the worker image sets `PROMETHEUS_MULTIPROC_DIR` (livekit-agents renders with a `MultiProcessCollector`). `/healthz`, `/readyz`, `/metrics` on the platform are unauthenticated.

## Tracing (OpenTelemetry)

Set an OTLP endpoint and the worker exports livekit-agents' `AgentSession` spans. `build_tracer_provider` runs once per job process in `_prewarm` and hands the provider to `livekit.agents.telemetry.set_tracer_provider`; with no endpoint set it returns `None` and tracing stays off.

| env var | default | meaning |
|---------|---------|---------|
| `OTEL_EXPORTER_OTLP_ENDPOINT` | *(empty)* | OTLP collector URL; empty disables tracing |
| `OTEL_EXPORTER_OTLP_PROTOCOL` | `http/protobuf` | `grpc` selects the gRPC exporter; any other value uses the HTTP exporter |
| `OTEL_SERVICE_NAME` | `voiceagent` | `service.name` resource attribute (the worker overrides it to `voiceagent-worker`) |

```bash
# HTTP/protobuf exporter (default) — typically port 4318
OTEL_EXPORTER_OTLP_ENDPOINT=http://collector:4318
# gRPC exporter — typically port 4317
OTEL_EXPORTER_OTLP_PROTOCOL=grpc
OTEL_EXPORTER_OTLP_ENDPOINT=http://collector:4317
```

## App metrics (`va_*`)

Emitted by the framework. The session/tool/cost metrics are driven from canonical events plus a once-per-session cost record inside the worker; `va_api_sessions_created_total` is incremented by the control plane when a session token is minted. Gate them with `METRICS_ENABLED` (default `true`).

| metric | type | labels | meaning |
|--------|------|--------|---------|
| `va_sessions_started_total` | counter | `agent_id`, `channel` | agent actually joined a session |
| `va_sessions_ended_total` | counter | `agent_id`, `channel`, `reason` | session ended (`reason` defaults to `completed`) |
| `va_session_duration_seconds` | histogram | `agent_id`, `channel` | wall-clock duration; buckets 1…1800 s |
| `va_tool_calls_total` | counter | `agent_id`, `tool`, `status` | tool invocations; `status` is `ok` or `error` |
| `va_usage_units_total` | counter | `provider`, `unit` | raw provider usage units consumed |
| `va_cost_usd_total` | counter | `agent_id` | estimated provider cost, USD |
| `va_api_sessions_created_total` | counter | `agent_id`, `channel` | sessions created by the control plane (tokens minted — not every one connects) |

`va_sessions_started_total` (worker: agent joined) is deliberately distinct from `va_api_sessions_created_total` (control plane: token minted); their difference is your connect-failure gap.

## Runtime metrics (`lk_agents_*`)

Exported by livekit-agents on the worker port — the framework does not define these. They report worker load and job counts, e.g. `lk_agents_worker_load` and `lk_agents_active_job_count`. This is the signal that sizes a fleet: a worker whose load crosses `WORKER_LOAD_THRESHOLD` (0.7) self-marks unavailable and LiveKit stops dispatching to it. See [load-testing](load-testing.md).

```bash
curl -s localhost:9100/metrics | grep lk_agents_
```

The complete set is whatever your livekit-agents version exports; scrape the endpoint to enumerate it.

## Usage → cost

Each channel feeds normalized usage into a `UsageCollector` (`add`, `merge` for deltas, `replace` for cumulative snapshots). At session end the worker calls `finalize()`, which returns the raw quantities plus a priced total from a built-in rate table, then:

- records `va_usage_units_total` / `va_cost_usd_total` (once, via `record_cost`),
- writes `cost_usd` to the status store, and
- persists `usage` + `cost_usd` to the session row (queryable via the status API).

Prices are keyed by `(provider, unit)` in USD per single unit — tokens per token (the per-1M rate ÷ 1e6), STT audio per second, TTS per character. Realtime providers split tokens into `input_tokens_text` / `input_tokens_audio` / `output_tokens_text` / `output_tokens_audio`. Unknown `(provider, unit)` pairs cost 0 but stay visible in the units, so unmetered spend is never hidden.

Built-in providers: `google`, `google-realtime`, `openai`, `openai-realtime`, `deepgram`, `cartesia`, `elevenlabs`. Rates were checked against provider pricing pages on 2026-09-06 — providers reprice without notice, so pin your own with env overrides:

```bash
# PRICE_<PROVIDER>_<UNIT>=<usd_per_unit>  (hyphens in provider → underscores)
PRICE_DEEPGRAM_AUDIO_SECONDS=0.0000722
PRICE_GOOGLE_REALTIME_INPUT_TOKENS_AUDIO=0.0000021
PRICE_ELEVENLABS_CHARACTERS=0.00003
```

Raw quantities are always stored, so past sessions can be re-priced retroactively after an override change.

## See also

- [configuration](configuration.md) — all env vars
- [recording](recording.md) — Egress audio capture
- [load-testing](load-testing.md) — reading `lk_agents_*` load to size a fleet
- [deploy-local](deploy-local.md), [deploy-k8s](deploy-k8s.md)
