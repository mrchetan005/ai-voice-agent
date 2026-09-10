# Load testing

`driver.py` opens N concurrent sessions against the platform API and measures
**time-to-first-agent-audio (TTFA)** under load — the metric that actually
sizes a worker fleet — plus success rate. Against the `mock-demo` agent it costs
nothing (no provider calls), so you can characterise capacity for free.

## Run it

Bring up the compose stack first:

```bash
docker compose -f deploy/compose/docker-compose.yml up --build -d
uv run python deploy/loadtest/driver.py \
  --platform http://localhost:8080 --token devtoken \
  --agent mock-demo --sessions 20 --ramp 5 --hold 10 \
  --metrics-url http://localhost:9100/metrics
```

| flag | meaning |
|------|---------|
| `--sessions` | number of concurrent sessions |
| `--ramp` | spread session starts over this many seconds (0 = all at once) |
| `--hold` | seconds each session keeps speaking |
| `--timeout` | per-session first-audio timeout |
| `--metrics-url` | worker `/metrics` to scrape after the run |

Sample output:

```
=== loadtest: 20 sessions vs 'mock-demo' ===
success:   20/20 (100%)
ttfa p50:  0.42s
ttfa p95:  0.71s
ttfa max:  0.83s
```

## Sizing a fleet

The real overload guard is each worker's `load_threshold` (0.7): a saturated
worker self-marks unavailable and LiveKit stops dispatching to it. So the
question isn't "what CPU %" but **how many concurrent sessions one worker holds
before its load crosses 0.7**.

1. Run with one worker replica and step `--sessions` up (10 → 20 → 40 …).
2. Watch `lk_agents_*` load in the scraped metrics (and TTFA p95 — the knee is
   where p95 climbs sharply).
3. That knee is **sessions-per-worker**. Set `worker.autoscaling.maxReplicas`
   to `peak_concurrent_sessions / sessions_per_worker`, with headroom.

CPU HPA (target 60%) only adds capacity *behind* the load-threshold backpressure
— it is not the primary scaler. See `deploy/k8s/README.md` (plan risk #6) for the
custom-metric upgrade path.
