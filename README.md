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

The repo also ships a production application built on the library:
an appointment-booking agent that runs real WhatsApp voice calls
(inbound and outbound) and text chats, and books into Cal.com. See
[Appointment booker](#appointment-booker) below.

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

## Appointment booker

`src/appointment_booker/` — a scheduling agent ("Priya") on top of the
library. It calls users on WhatsApp (or answers their calls), holds a
natural conversation in English/Hindi/Hinglish and other Indian languages,
books the agreed slot in Cal.com, and sends the confirmation as a WhatsApp
message. Text chat works too, and both channels share one conversation
history per caller, persisted in Postgres.

Moving parts:

- Meta WhatsApp Business Calling API for call signalling, aiortc for the
  WebRTC media leg
- Gemini Live as voice and brain in one (`--brain single`, default, lowest
  latency); optional `--brain dual` mode routes replies through a
  checkpointed LangGraph agent instead
- Cal.com v2 API for availability and bookings
- Postgres (Neon) for transcripts and cross-channel memory — written
  asynchronously, never blocking the audio path

### Setup

1. Meta app with WhatsApp Business Calling enabled; note the phone number ID
   and access token.
2. Expose the webhook port publicly (`ngrok http 8080` during development)
   and register the URL + `WHATSAPP_VERIFY_TOKEN` in the Meta App dashboard,
   subscribed to both `calls` and `messages` fields.
3. Cal.com API key and an event type ID
   (`uv run --env-file .env python -m appointment_booker.cal_client` lists
   yours).
4. Postgres connection string in `DATABASE_URL` (optional — without it the
   agent runs memory-only and warns).
5. Fill the rest of `.env` from `.env.example`.

### Run

```sh
uv run --env-file .env appointment-booker            # outbound: call the user
uv run --env-file .env appointment-booker --inbound  # answer calls to the business number
uv run --env-file .env appointment-booker --chat     # book over WhatsApp text
```

Useful flags: `--brain single|dual`, `--skip-permission` (reuse a call
permission granted in the last 7 days), `--serve-only` (webhook server only),
`--port` (default 8080). Outbound calls first send a call-permission request
the user must accept — that is Meta policy, not this app.

### Docker

```sh
docker build -t appointment-booker .
docker run --env-file .env -p 8080:8080 appointment-booker --inbound
```

Secrets are passed at runtime via `--env-file`; nothing is baked into the
image.

## Development

```sh
uv run tests/test_ws_e2e.py                        # WebSocket end-to-end (mock engine)
uv run --env-file .env tests/test_gemini_live.py   # live Gemini session (needs GOOGLE_API_KEY)
uv run python -m voiceagent.guardrails_and_eval    # eval harness
uvx ruff check src tests examples                  # lint (config in pyproject.toml)
```

## Project layout

```
src/voiceagent/                  the library
  models.py                      pydantic schemas: session config, pulses, approvals
  base.py                        transports, bounded queues, phrase cache, base proxy
  providers.py                   OpenAI Realtime / Gemini Live / split-stack / mock
  commentary_and_approval.py     live commentary, approval gateway, agent bridge
  guardrails_and_eval.py         injection/PII guardrails, telemetry, eval harness
  adapters.py                    LangChain / CrewAI / WebSocket / HTTP adapters
src/appointment_booker/          the WhatsApp booking application
  main.py                        entry point and call/chat orchestration
  prompts.py                     every conversation prompt, in one place
  graph.py                       LangGraph booking agent (chat + dual-brain voice)
  native_tools.py                Gemini-native tools, transcript store, chat<->call bridge
  transport_whatsapp.py          aiortc WebRTC transport for Meta calling
  webhooks.py                    Meta webhook receiver (calls + messages)
  whatsapp_api.py                WhatsApp Cloud API client
  cal_client.py                  Cal.com v2 client
```
