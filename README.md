# voiceagent

Provider-agnostic realtime voice pipeline for Python. Plug any backend agent
— a plain async function, LangChain/LangGraph, CrewAI, or a remote WS/HTTP
service — into a spoken conversation in a few lines, and swap the underlying
speech stack (OpenAI Realtime, Gemini Live, or a Deepgram/Groq/Cartesia
split stack) without touching agent code.

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

The repo also ships a production application built on the library: a
WhatsApp assistant that handles text chats and real voice calls (inbound
and outbound) for people who'd rather talk than type. Appointment booking
into Cal.com is its first capability; more capabilities plug in as sibling
packages. See [WhatsApp agent](#whatsapp-agent) below.

## Requirements

- Python 3.11+
- [uv](https://docs.astral.sh/uv/) (any PEP 517 installer works; commands
  below assume uv)

## Installation

```sh
uv sync                        # library + dev environment
uv add "voiceagent[local]"     # mic/speaker transport (sounddevice)
uv add "voiceagent[booking]"   # appointment booker deps (LangGraph, aiortc, psycopg)
```

## Quick start (no API keys)

```sh
uv run examples/quickstart.py --mock              # offline conversation demo
uv run python -m voiceagent.guardrails_and_eval   # eval harness, 15 checks
```

## Providers

| Provider string | Stack |
|---|---|
| `openai-realtime` | OpenAI Realtime (`gpt-realtime-2.1`), strict-proxy mode |
| `gemini-live` | Gemini Live native audio; agent tools mounted directly in the session |
| `deepgram+groq+cartesia` | Deepgram nova-3 ASR → Groq LLM → Cartesia sonic TTS |
| `deepgram-flux+groq+elevenlabs` | Flux turn detection → Groq → ElevenLabs flash v2.5 |
| `mock` | Offline engine for tests and demos |

Split-stack parts combine freely: `{deepgram, deepgram-flux} + {groq, openai}
+ {cartesia, elevenlabs}`. Models and voices are overridable per part:
`VoiceAgent(..., model="...", tts_voice="...", asr_model="...")`.

## Transports

- `run("local")` — microphone and speakers
- `run("ws://0.0.0.0:8765")` — WebSocket server; binary PCM16 both ways,
  per-connection overrides via query params (`?language=ta-IN&tone=formal`)
- `run(my_transport)` — any object implementing
  `voiceagent.base.AudioTransport` (this is where WebRTC plugs in; the
  appointment booker's WhatsApp transport is an example)

## Plugging in an existing agent

```python
from voiceagent.adapters import from_langchain, from_crewai, from_websocket, from_http

VoiceAgent(provider="gemini-live", agent=from_langchain(my_graph)).run("local")
VoiceAgent(agent=from_websocket("ws://my-orchestrator:9000"), ...)
```

Remote services speak a small JSON contract (documented in
`src/voiceagent/adapters.py`): status pulses become spoken commentary,
`requires_approval` pulses block until the caller says yes or no, and the
verdict is posted back upstream.

## Configuration

API keys are read from conventional environment variables — only the ones
your provider string needs: `OPENAI_API_KEY`, `GOOGLE_API_KEY`,
`DEEPGRAM_API_KEY`, `GROQ_API_KEY`, `CARTESIA_API_KEY`, `ELEVENLABS_API_KEY`.

Session defaults can also come from env (`VOICEAGENT_LANGUAGE`,
`VOICEAGENT_VOICE_ID`, ...) — see `src/voiceagent/models.py::SessionConfig`.
Copy `.env.example` to `.env` and fill in what you use; `.env` is gitignored
and must never be committed.

## WhatsApp agent

`src/whatsapp_agent/` — a general WhatsApp assistant ("Priya") on top of the
library, running as ONE FastAPI service: Meta webhooks, inbound call
answering, outbound calls, text chat, and an admin API. It holds a natural
conversation in English/Hindi/Hinglish and other Indian languages over chat
or voice — voice exists for people who don't want to (or can't) type.

**Appointment booking is the first capability, not the only one.** Booking
lives in `capabilities/booking/`; a new capability (orders, support, FAQ…)
is a sibling package that contributes LangGraph tools to the brain and
native declarations to the voice tool set — two registration points, no
plugin framework.

Moving parts:

- Meta WhatsApp Business Calling API for signalling, aiortc for the WebRTC
  media leg, X-Hub-Signature-256 validation on every webhook
- Gemini Live as voice+brain in one (`single` brain, lowest latency), or any
  engine (`openai-realtime`, `split` = Deepgram + Cartesia) in `dual` mode
  behind the checkpointed LangGraph agent (LLM swappable via
  `SCHEDULER_MODEL`: gemini / groq / openai / openrouter BYOK / LiteLLM)
- Cal.com v2 API for availability and bookings
- Postgres (Neon) for transcripts, profiles, bookings, session records —
  written asynchronously, never blocking the audio path
- Redis for webhook dedup, per-phone rate limits, slots/profile caches and
  call locks — every operation fail-open: Redis down never breaks a call

### Setup

1. Meta app with WhatsApp Business Calling enabled; note the phone number
   ID, access token and **app secret** (`WHATSAPP_APP_SECRET`).
2. A public HTTPS URL to port 8080 — `ngrok http 8080` in development,
   Caddy + your domain in production — registered in the Meta App dashboard
   with `WHATSAPP_VERIFY_TOKEN`, subscribed to `calls` and `messages`.
3. Cal.com API key and an event type ID
   (`uv run --env-file .env python -m whatsapp_agent.capabilities.booking.cal_client`
   lists yours).
4. Postgres in `DATABASE_URL` and Redis in `REDIS_URL` (both optional — the
   agent degrades gracefully and warns).
5. Fill the rest of `.env` from `.env.example`.

### Run (development)

```sh
uv run --env-file .env whatsapp-agent serve     # everything: webhooks + calls + chat
uv run --env-file .env whatsapp-agent call      # place one outbound test call
uv run --env-file .env whatsapp-agent call --provider split --voice kavita --brain dual
uv run --env-file .env whatsapp-agent inbound   # answer calls only
uv run --env-file .env whatsapp-agent chat      # text chat only
uv run --env-file .env whatsapp-agent audit     # sampled hallucination audit
```

Outbound calls first send a call-permission request the user must accept
(Meta policy); `--skip-permission` reuses a grant from the last 7 days.
Chat sessions never self-exit — a background sweep flushes sessions idle
for 30+ minutes into the session store.

### HTTP API

| Endpoint | Auth | Purpose |
|---|---|---|
| `GET/POST /webhook` | Meta signature | webhook verify + signed event intake |
| `GET /health` | none | `{status, redis, db, active_call, version}` |
| `GET /config` | bearer | every runtime key with `{value, source}` |
| `POST /config` | bearer | change engine settings live, e.g. `{"provider": "split", "voice": "kavita"}`; `null` clears an override |
| `POST /calls` | bearer | start an outbound call: 202 `{call_ref}`, `Idempotency-Key` replay 200, 409 when busy |
| `GET /report` | bearer | health/latency/outcomes/audit rollup (`?days=7`) |
| `GET /costs` | bearer | per-session and per-provider spend for the consumer agent |

Bearer = `ADMIN_TOKEN` (falls back to `METRICS_TOKEN`); with no token
configured these endpoints answer 404. Precedence for every engine setting:
request/CLI > runtime config (`POST /config`) > env. Example — switch the
next call to the split stack without touching `.env`:

```sh
curl -X POST https://$DOMAIN/config -H "Authorization: Bearer $ADMIN_TOKEN" \
     -d '{"provider": "split", "brain": "dual", "voice": "rohan"}'
curl -X POST https://$DOMAIN/calls -H "Authorization: Bearer $ADMIN_TOKEN" \
     -H "Idempotency-Key: crm-42" -d '{"to": "91XXXXXXXXXX"}'
```

### Deploy (production)

The stack needs a VPS/VM with Docker — WebRTC media requires outbound UDP
and one long-lived process, so serverless platforms (Cloud Run, Lambda)
are unsuitable. Open 80/443 inbound; point your domain at the box.

```sh
cp .env.example .env            # fill it; set WHATSAPP_APP_SECRET
DOMAIN=agent.example.com docker compose up -d --build
```

That starts the app, Redis, and Caddy with automatic HTTPS; the Meta
webhook URL becomes `https://agent.example.com/webhook`. The app runs
exactly ONE uvicorn worker by design (in-process media and queues) —
scale later means Redis pub/sub between processes, not `--workers 2`.
ngrok is for local development only.

## Development

```sh
uv run pytest                                      # offline suite (no keys, no network)
uv run --env-file .env pytest -m live              # live tests (real APIs/DB; costs money)
uv run python -m voiceagent.guardrails_and_eval    # eval harness
uvx ruff check src tests examples                  # lint (config in pyproject.toml)
```

## Project layout

```
src/voiceagent/                  the library (public API frozen)
  models.py                      pydantic schemas: session config, pulses, approvals
  base.py                        transports, bounded queues, phrase cache, base proxy
  providers/                     openai_realtime / gemini_live / split_stack / asr / tts / llm / mock
  commentary_and_approval.py     live commentary, approval gateway, agent bridge
  telemetry.py, guardrails.py, eval_harness.py
  adapters.py                    LangChain / CrewAI / WebSocket / HTTP adapters
src/whatsapp_agent/              the WhatsApp assistant application
  cli.py                         serve / call / inbound / chat / audit
  config.py                      pydantic-settings: every env var, one class
  api/                           FastAPI factory + routes (webhooks, health, admin, config, calls)
  channels/                      Meta plumbing: events/router, WhatsApp client,
                                 WebRTC transport, call + chat managers
  agent/                         LangGraph brain, prompts, recap, audit
  capabilities/booking/          FIRST capability: booking service, voice tools, Cal.com client
  infra/                         stores (Postgres), redis gateway, runtime config,
                                 pricing, metering
```
