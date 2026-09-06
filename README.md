# AI Voice Agent

A WhatsApp assistant ("Priya") that talks to your customers over **text chat
and real voice calls** — inbound and outbound — in English, Hindi, Hinglish
and other Indian languages, and books appointments into Cal.com. Built on
`voiceagent`, a provider-agnostic realtime voice library that also ships in
this repo.

```
WhatsApp user ──text──▶ ┌─────────────────────────────┐
              ──call──▶ │  whatsapp_agent (FastAPI)   │──▶ Cal.com bookings
Meta webhooks ────────▶ │  webhooks·calls·chat·admin  │──▶ Postgres (memory)
your tools ──HTTP API─▶ └──────────┬──────────────────┘──▶ Redis (fail-open)
                                   ▼
                        voiceagent library
             (Gemini Live | OpenAI Realtime | Deepgram+Cartesia)
```

**Contents:** [Quick start](#quick-start-first-test-in-5-minutes) ·
[Voice engines](#voice-engines--brains) · [CLI](#cli-reference) ·
[HTTP API](#http-api--runtime-config) · [Observability](#observability) ·
[Deploy](#deploy-production) · [Adding capabilities](#adding-a-capability) ·
[The library](#the-voiceagent-library) · [Development](#development) ·
[Layout](#project-layout)

Two entities, one repo:

| | What | Where |
|---|---|---|
| **whatsapp_agent** | The application: one FastAPI service running Meta webhooks, inbound call answering, outbound calls, text chat and an admin API. Appointment booking is its **first** capability, not the only one. | `src/whatsapp_agent/` |
| **voiceagent** | The library: plug any agent (async function, LangChain/LangGraph, CrewAI, remote WS/HTTP) into a spoken conversation; swap speech stacks without touching agent code. | `src/voiceagent/` |

---

## Quick start (first test in ~5 minutes)

Prerequisites: Python 3.11+, [uv](https://docs.astral.sh/uv/), a Meta app
with WhatsApp Business Calling enabled, a Cal.com account.

**1. Install**

```sh
uv sync --extra booking
```

**2. Configure** — `cp .env.example .env`, then fill at minimum:

| Key | Where to get it |
|---|---|
| `WHATSAPP_ACCESS_TOKEN`, `WHATSAPP_PHONE_NUMBER_ID` | Meta App dashboard → WhatsApp → API setup |
| `WHATSAPP_RECIPIENT` | your test phone, E.164 without `+` (e.g. `91XXXXXXXXXX`) |
| `WHATSAPP_APP_SECRET` | Meta App → Settings → Basic (**required in production**; unset = signature check off, dev only) |
| `GOOGLE_API_KEY` | Gemini (default voice engine + brain) |
| `CAL_API_KEY`, `CAL_EVENT_TYPE_ID` | Cal.com; list your event types with `uv run --env-file .env python -m whatsapp_agent.capabilities.booking.cal_client` |
| `DATABASE_URL` | any Postgres (Neon works); optional — without it the agent runs memory-only and warns |
| `METRICS_TOKEN` | any random string; unlocks `/report` `/costs` `/config` `/calls` |

Split-stack testing additionally needs `DEEPGRAM_API_KEY`, `GROQ_API_KEY`,
`CARTESIA_API_KEY`.

**3. Expose the webhook** (its own terminal, keep running):

```sh
ngrok http 8080        # DEV ONLY — production uses Caddy + your domain
```

In the Meta App dashboard → WhatsApp → Configuration set the callback URL to
`https://<your-ngrok-id>.ngrok-free.app/webhook`, verify token =
`WHATSAPP_VERIFY_TOKEN` (default `voiceagent`), and subscribe to the
**calls** and **messages** fields.

**4. Run everything:**

```sh
uv run --env-file .env whatsapp-agent serve
```

**5. Test any channel:**

- **Chat** — WhatsApp-message the business number: *"book me a slot tomorrow
  afternoon"*. The agent checks real availability, asks for your email,
  sends Confirm/Edit buttons, books on Confirm.
- **Inbound call** — open the business chat in WhatsApp and tap the call
  button. Priya picks up.
- **Outbound call** — in another terminal:
  `uv run --env-file .env whatsapp-agent call`
  (sends a call-permission request first — Meta policy — accept it on your
  phone; `--skip-permission` reuses a grant from the last 7 days).

Both channels share one conversation history per phone number, so Priya
remembers chat context on a call and vice versa. After every meaningful
call she sends a WhatsApp **recap** message.

---

## Voice engines & brains

| `--provider` | Brain modes | Stack |
|---|---|---|
| `gemini-live` (default) | `single` (default) or `dual` | Gemini Live native audio; in `single` mode it is voice + brain in one, tools mounted natively — lowest latency |
| `openai-realtime` | `dual` (forced) | OpenAI Realtime (`gpt-realtime-2.1`) as strict voice front-end |
| `split` | `dual` (forced) | Deepgram nova-3 **multilingual** ASR → brain LLM → Cartesia TTS |

- **single brain**: Gemini Live calls the booking tools directly.
- **dual brain**: the voice engine relays to a checkpointed LangGraph agent;
  its LLM is swappable via `SCHEDULER_MODEL`:
  `gemini-3.6-flash` · `groq/openai/gpt-oss-20b` ·
  `openai/gpt-4o-mini` · `openrouter/<any model>` (BYOK) ·
  `custom/<alias>` + `SCHEDULER_BASE_URL` (LiteLLM or any OpenAI-compatible
  gateway).
- **Voices**: `--voice rohan` / `--voice kavita` (Cartesia aliases), any raw
  Cartesia id, Gemini prebuilt names (e.g. `Despina`), or OpenAI voices.
- Inbound calls always answer as gemini-live single-brain.

## CLI reference

Every command runs the same FastAPI app as production; they differ only in
which managers start.

```sh
uv run --env-file .env whatsapp-agent serve      # webhooks + inbound + chat (= production)
uv run --env-file .env whatsapp-agent call       # one outbound call, then exit
uv run --env-file .env whatsapp-agent call --provider split --brain dual --voice kavita
uv run --env-file .env whatsapp-agent inbound    # answer calls only
uv run --env-file .env whatsapp-agent chat       # text chat only
uv run --env-file .env whatsapp-agent audit --days 7 --sample 10   # hallucination audit
```

Chat sessions never self-exit; a background sweep flushes sessions idle for
30+ minutes into the session store.

## HTTP API & runtime config

| Endpoint | Auth | Purpose |
|---|---|---|
| `GET/POST /webhook` | Meta signature | verify handshake + signed event intake |
| `GET /health` | none | `{status, redis, db, active_call, version}` — used by Docker healthcheck |
| `GET /config` | bearer | every runtime key with `{value, source}` |
| `POST /config` | bearer | change engine settings live; `null` clears an override |
| `POST /calls` | bearer | start an outbound call: `202 {call_ref}`, `Idempotency-Key` replay → 200, busy → 409 |
| `GET /report?days=7` | bearer | volume, outcomes, conversion, latency percentiles, flags, audit scores |
| `GET /costs?days=30` | bearer | per-session and per-provider USD spend (built for a consumer agent) |

Bearer token = `ADMIN_TOKEN`, falling back to `METRICS_TOKEN`; with neither
set, these endpoints answer 404 (indistinguishable from not existing — they
share the public ingress with the Meta webhook).

Every engine setting resolves **request/CLI → runtime config → env**, so
you never edit `.env` to switch engines:

```sh
# make the NEXT call use the split stack with Rohan's voice
curl -X POST http://localhost:8080/config \
     -H "Authorization: Bearer $METRICS_TOKEN" \
     -d '{"provider": "split", "brain": "dual", "voice": "rohan"}'

# start a call from anywhere (CRM hook, consumer agent, curl)
curl -X POST http://localhost:8080/calls \
     -H "Authorization: Bearer $METRICS_TOKEN" \
     -H "Idempotency-Key: crm-42" -d '{"to": "91XXXXXXXXXX"}'
```

Allowed runtime keys: `provider`, `brain`, `voice`, `scheduler_model`,
`split_asr`, `split_asr_model`, `split_asr_language`, `split_tts`,
`split_tts_model`, `recap_enabled`.

## Observability

- **Is it working?** `GET /report` — call/chat volume, booking conversion,
  drop-off rate, latency percentiles, deterministic quality flags.
- **Is it hallucinating?** Deterministic flags on every session plus a
  sampled LLM-judge audit: `whatsapp-agent audit` (verdicts land in
  `/report`).
- **What does it cost?** Every provider is metered per session (Gemini
  Live/Flash, OpenAI, Groq, Deepgram, Cartesia, WhatsApp messages);
  `GET /costs` returns the breakdown. Prices live in
  `src/whatsapp_agent/infra/pricing.py`, overridable via `PRICE_*` env vars.

## Deploy (production)

You need a **VPS/VM with Docker** — WebRTC media requires outbound UDP and
one long-lived process, so serverless (Cloud Run, Lambda) is unsuitable.
Open 80/443 inbound, point your domain's A record at the box.

```sh
cp .env.example .env                # fill it; set WHATSAPP_APP_SECRET!
DOMAIN=agent.example.com docker compose up -d --build
```

That starts **app + Redis + Caddy** (automatic HTTPS). The Meta webhook URL
becomes `https://agent.example.com/webhook`. Notes:

- The app runs exactly **one** uvicorn worker by design (in-process WebRTC
  media and queues). Scaling later = Redis pub/sub between processes, not
  `--workers 2`.
- Redis backs webhook dedup, per-phone rate limits, slots/profile caches
  and call locks — **every operation fails open**: a Redis outage never
  breaks a live call (verified by stopping Redis mid-run).
- `stop_grace_period: 30s` lets an active call finish its recap and session
  record on shutdown.
- ngrok is for local development only.

## Adding a capability

Booking lives in `src/whatsapp_agent/capabilities/booking/`. A new
capability (orders, support, FAQ…) is a **sibling package** with two
registration points — no plugin framework:

1. LangGraph `@tool`s added to the brain's tool list (`agent/brain.py`)
2. native tool declarations added to the voice tool dict
   (`capabilities/booking/voice_tools.py` is the pattern)

---

## The voiceagent library

Plug any backend agent into a spoken conversation in a few lines:

```python
from voiceagent import VoiceAgent

async def my_agent(ctx):
    await ctx.status("fetching_db_records")          # spoken live commentary
    if not await ctx.approve("Delete 45 records?"):  # blocking voice approval
        return "Okay, cancelled."
    return "Done!"

VoiceAgent(provider="deepgram+groq+cartesia", agent=my_agent,
           language="hi-IN", tone="warm").run("ws://0.0.0.0:8765")
```

```sh
uv run examples/quickstart.py --mock              # offline demo, no API keys
uv run python -m voiceagent.guardrails_and_eval   # eval harness, 15 checks
```

**Providers:** `openai-realtime` · `gemini-live` ·
`deepgram+groq+cartesia` · `deepgram-flux+groq+elevenlabs` · `mock`.
Split-stack parts combine freely (`{deepgram, deepgram-flux} × {groq,
openai} × {cartesia, elevenlabs}`) with per-part model/voice overrides.

**Transports:** `run("local")` (mic/speakers, needs `voiceagent[local]`),
`run("ws://...")` (WebSocket server, binary PCM16), or any object
implementing `voiceagent.base.AudioTransport` — the WhatsApp WebRTC
transport in this repo is the reference example.

**Adapters:** `from_langchain`, `from_crewai`, `from_websocket`,
`from_http` (JSON contract documented in `src/voiceagent/adapters.py`).
Status pulses become spoken commentary; `requires_approval` pulses block
until the caller says yes or no.

API keys are conventional env vars, read only when a provider needs them:
`OPENAI_API_KEY`, `GOOGLE_API_KEY`, `DEEPGRAM_API_KEY`, `GROQ_API_KEY`,
`CARTESIA_API_KEY`, `ELEVENLABS_API_KEY`. The library's public API is
frozen — the app depends on it, internals are reorganized behind shims.

## Development

```sh
uv run pytest                              # offline suite (no keys, no network)
uv run --env-file .env pytest -m live      # live tests: real APIs + DB (costs money)
uvx ruff check src tests examples          # lint (config in pyproject.toml)
```

`.env` is gitignored and must never be committed.

## Project layout

```
src/voiceagent/                  the library (public API frozen)
  models.py                      pydantic schemas: session config, pulses, approvals
  base.py                        transports, bounded queues, phrase cache, base proxy
  providers/                     openai_realtime / gemini_live / split_stack / asr / tts / llm / mock
  commentary_and_approval.py     live commentary, approval gateway, agent bridge
  telemetry.py · guardrails.py · eval_harness.py
  adapters.py                    LangChain / CrewAI / WebSocket / HTTP adapters
src/whatsapp_agent/              the WhatsApp assistant application
  cli.py                         serve / call / inbound / chat / audit
  config.py                      pydantic-settings: every env var, one class
  api/                           FastAPI factory + routes (webhooks, health, admin, config, calls)
  channels/                      Meta plumbing: event router, WhatsApp client,
                                 WebRTC transport, call + chat managers
  agent/                         LangGraph brain, prompts, recap, audit
  capabilities/booking/          FIRST capability: booking service, voice tools, Cal.com client
  infra/                         stores (Postgres), Redis gateway, runtime config,
                                 pricing, metering
tests/                           unit/ (offline, default) + live/ (opt-in, -m live)
```
