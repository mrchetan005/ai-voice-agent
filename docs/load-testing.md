# Load testing

`deploy/loadtest/driver.py` opens N concurrent sessions against the platform API and measures **time-to-first-agent-audio (TTFA)** under load — the metric that actually sizes a worker fleet — plus success rate, then optionally scrapes a worker's `lk_agents_*` load.

Against the `mock-demo` agent it makes no provider calls, so you can characterise capacity for **$0**.

## Run it

Bring up the compose stack, then drive it:

```bash
docker compose -f deploy/compose/docker-compose.yml up --build -d
uv run python deploy/loadtest/driver.py \
  --platform http://localhost:8080 --token devtoken \
  --agent mock-demo --sessions 20 --ramp 5 --hold 10 \
  --metrics-url http://localhost:9100/metrics
```

Requires the `agents`/`platform` extras (livekit, httpx).

| flag | default | meaning |
|------|---------|---------|
| `--platform` | `http://localhost:8080` | platform base URL |
| `--token` | `devtoken` | `PLATFORM_API_TOKEN` bearer |
| `--agent` | `mock-demo` | `agent_id` to create sessions for |
| `--sessions` | `10` | number of concurrent sessions |
| `--ramp` | `0` | spread session starts over this many seconds (0 = all at once) |
| `--hold` | `10` | seconds each session keeps speaking (keeps calls genuinely concurrent) |
| `--timeout` | `60` | per-session first-audio timeout |
| `--metrics-url` | *(empty)* | worker `/metrics` to scrape after the run |

## What it measures

Each session times `t0` at the `POST /v1/sessions` call, connects a raw WebRTC client, waits for the agent to be dispatched into the room, publishes a fixture utterance on a loop, and records **TTFA** as the moment the first non-silent agent audio frame arrives (RMS > 200). It holds the call for `--hold` seconds so N sessions overlap, then reports:

```
=== loadtest: 20 sessions vs 'mock-demo' ===
success:   20/20 (100%)
ttfa p50:  0.42s
ttfa p95:  0.71s
ttfa max:  0.83s
```

Failures are printed per session (create rejected, agent never joined, no agent audio before timeout). The driver exits `0` only if every session succeeded, else `1` — usable as a CI gate.

With `--metrics-url` it scrapes the worker afterwards and prints the `lk_agents_*` load/job lines (see [observability](observability.md)).

## Sizing a fleet

The real overload guard is each worker's `WORKER_LOAD_THRESHOLD` (0.7): a saturated worker self-marks unavailable and LiveKit stops dispatching to it. So the question isn't "what CPU %" but **how many concurrent sessions one worker holds before its load crosses 0.7**.

1. Run **one** worker replica and step `--sessions` up: 10 → 20 → 40 …
2. Watch `lk_agents_*` load in the scraped metrics **and** TTFA p95 — the knee is where p95 climbs sharply.
3. That knee is **sessions-per-worker**. Set
   `maxReplicas = peak_concurrent_sessions / sessions_per_worker`, with headroom.

Divide observed concurrency by per-worker load rather than guessing from CPU. A CPU HPA (e.g. target 60%) only adds capacity *behind* the load-threshold backpressure — it is not the primary scaler.

```bash
# one worker, climbing steps
for n in 10 20 40 80; do
  uv run python deploy/loadtest/driver.py --sessions "$n" --ramp 5 --hold 15 \
    --metrics-url http://localhost:9100/metrics
done
```

## Against a real cluster

Point `--platform` and `--token` at the deployed control plane and `--metrics-url` at a worker's metrics port. Note that non-mock agents make real provider calls (metered — see [observability](observability.md)); keep runs short. See [deploy-k8s](deploy-k8s.md) for `worker.autoscaling` and the custom-metric upgrade path.

## See also

- [observability](observability.md) — `lk_agents_*` load metrics, `va_*` app metrics
- [deploy-local](deploy-local.md), [deploy-k8s](deploy-k8s.md), [deploy-gke](deploy-gke.md), [deploy-eks](deploy-eks.md)
- [configuration](configuration.md) — `WORKER_LOAD_THRESHOLD`, `WORKER_PROMETHEUS_PORT`
