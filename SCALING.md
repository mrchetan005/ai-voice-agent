# Scaling the voice agent

Short reference for taking outbound calling from today's setup to ~100k
users/month. Nothing here requires restructuring — the seams already exist.

## Where we are

- One FastAPI process, **one uvicorn worker** (in-process WebRTC + queues).
- Concurrency: `max_concurrent_calls` capacity gate (default 3, switchable
  live via `POST /config`), per-peer Redis lock `call:active:{phone}`,
  one-session-per-peer guard in the EventRouter.
- `POST /calls` takes a booking **brief** (topic, time window, attendee);
  `GET /calls/{ref}` reads fail-open `call:status:{ref}` Redis keys:
  `accepted → permission_pending? → dialing → ringing → in_progress →
  completed | failed | no_answer | permission_denied`.

## Capacity math (100k users/month)

- 100k/mo ≈ 3,300 calls/day ≈ 300–1,000 dial attempts/hour at peak
  (business-hours skew, ~50% permission-grant × ~70% answer funnel).
- One call occupies a slot ~5 min (talk + setup + recap) → 1 slot ≈ 10–12
  calls/hour. Needed concurrency = peak rate × occupancy:
  500/hr → ~40 slots; 1,000/hr → ~85.
- One Python process running aiortc/Opus handles **~10–15 concurrent calls**
  (~25–35k completed calls/month). Beyond that: queue + worker fleet.

## Phase 2: durable queue (Postgres)

One `call_jobs` table; the API INSERTs, a dispatcher task dials.

```sql
CREATE TABLE call_jobs (
  id bigserial PRIMARY KEY,
  peer text NOT NULL,
  brief jsonb NOT NULL DEFAULT '{}',
  status text NOT NULL DEFAULT 'queued',   -- queued|leased|done|failed
  attempts int NOT NULL DEFAULT 0,
  next_attempt_at timestamptz NOT NULL DEFAULT now(),
  lease_expires_at timestamptz,
  claimed_by text,
  idem_key text UNIQUE,
  created_at timestamptz NOT NULL DEFAULT now()
);
```

- Claim: `SELECT ... WHERE status='queued' AND next_attempt_at <= now()
  ORDER BY id FOR UPDATE SKIP LOCKED LIMIT 1` — multi-worker-safe from day
  one. Lease + heartbeat; expired leases requeue (crash recovery).
- **Retry policy: max 2 attempts, 2nd after ~3 h.** Meta warns the user
  after 2 consecutive unanswered business-initiated calls and revokes the
  calling permission after 4 (production). Track a per-peer unanswered
  streak and hard-stop at 2.
- Calling-hours window checked at claim time (e.g. 10:00–19:00 IST);
  overnight jobs just wait in the queue.

### The plug-in seams (why no restructuring)

- `POST /calls` switches from `await calls.start_outbound(...)` to an
  INSERT; the dispatcher calls the **same** `start_outbound`. `CallBusy`
  ("at capacity") → back off and retry the claim.
- `call:status:{ref}` keys and `Idempotency-Key` handling already exist;
  the job's `idem_key` maps onto them.

## Phase 3: multiple call workers

- N worker processes each own their WebRTC legs and claim from the same
  `call_jobs` table (SKIP LOCKED distributes work for free).
- Webhook ingress stays one process and fans call events out over Redis
  pub/sub keyed by peer/call_id; each worker's EventRouter subscribes to
  its own calls. The per-peer Redis lock is already cross-process.
- 4 workers × ~12 concurrent ≈ 50 slots → 100k/mo with headroom.

## Managed queues (SQS / Cloud Tasks) — when and why not

- Either works, but a call job needs a queryable DB row anyway (status API,
  attempts, strike counters, audit) — a managed queue **adds** a system
  and a failure domain rather than replacing one, at ~0.04 jobs/sec.
- **SQS**: max delivery delay 15 min → the 3 h retry needs a scheduler tier
  (EventBridge) on top. **Cloud Tasks**: native scheduling up to 30 days
  and per-queue rate caps, but push-model + GCP coupling.
- Swap point if ever needed: only the dispatcher changes; `start_outbound`,
  status keys and idempotency stay identical.

## Meta quota guards (enforce in the dispatcher)

- Permission requests: **1/day, 2/week per user**; a grant lasts ~7 days.
- Connected calls: 100/day per business+user pair (Dec 2025 limits).
- Eligibility: the business number needs the 2,000-recipients/day
  messaging tier before business-initiated calling can be enabled.
- Best connect moment: dial immediately on the permission-grant webhook
  (proof the user is online). For standing permission, send a heads-up
  text and wait for the `delivered` receipt before dialing.

## Observability checklist

Queue depth + oldest-job age (the SLA alarm) · dial→connect rate ·
permission grant rate · per-peer unanswered streaks · Graph API error
codes (131030, 138017, …) · active slots vs `max_concurrent_calls` ·
terminal-status histogram from `call:status:*`.
