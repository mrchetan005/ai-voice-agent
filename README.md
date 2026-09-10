# voiceagent

A reusable voice-agent framework on **self-hosted LiveKit**. Define an agent in a few
lines (or in YAML), pick your providers, and connect the *same* agent to browser
WebRTC, SIP/PSTN, or WhatsApp Calling — then deploy it at scale on Kubernetes.

```python
from voiceagent import VoiceAgent, run, tool, ToolContext

@tool(description="Look up the price for a SKU")
async def check_price(ctx: ToolContext, sku: str) -> str:
    return f"{sku} costs $42"

agent = VoiceAgent(
    name="sales-agent",
    llm="google", model="gemini-2.5-flash",
    stt="deepgram", tts="cartesia",
    system_prompt="You are a concise, friendly sales assistant for {company}.",
    tools=[check_price],
    language="en",
)

if __name__ == "__main__":
    run(agent)   # dev / start / console
```

Swapping to a realtime model is a config change, not an architecture change:

```python
agent = VoiceAgent(name="sales-agent", mode="realtime", llm="google",
                   model="gemini-2.0-flash-live-001", system_prompt="...")
```

## Architecture

```
Channels (browser / SIP / WhatsApp)
        │
        ▼
   LiveKit (self-hosted SFU + SIP + Egress)
        │
        ▼
 Agent workers (livekit-agents runtime)
        │
        ▼
 AgentSession ── pipeline: STT → LLM → TTS
             └── realtime: Gemini Live / OpenAI Realtime
        │
        ▼
 Tools · Memory (Redis/Postgres) · Usage → cost · Recording (Egress)
```

Three layers, one rule:

- **`voiceagent`** (core): config, prompts, tools, memory, events, provider
  registry — zero `livekit` imports (enforced by a test).
- **`voiceagent.livekit`**: the only package importing `livekit*` — provider
  plugin factories, the config→AgentSession compile seam, the worker runtime.
- **`voiceagent.platform` / `voiceagent.channels`**: FastAPI control plane and
  channel adapters (browser tokens, SIP trunks, WhatsApp bridge).

## Status

Under active development on this branch. See `docs/` for the quickstart,
concepts, provider/channel guides, and Kubernetes deployment.

## Development

```bash
uv sync --all-extras          # install everything
uv run pytest                 # offline suite (no keys, no network)
uv run ruff check src tests
```

Markers: default runs exclude `live` (real provider APIs) and `integration`
(needs the local compose stack — still $0).
