# Quickstart

From zero to a talking agent. The fastest path costs nothing: mock providers
prove the full media path without a single API key.

## Install

```bash
uv sync --all-extras          # everything: agents, platform, whatsapp, obs
# or pick extras:
#   uv sync --extra agents     # livekit-agents runtime + provider plugins
#   uv sync --extra platform   # FastAPI control plane (Redis, Postgres)
```

Three console scripts are installed: `voiceagent-worker` (the agent runtime),
`voiceagent-api` (the control plane), and `voiceagent-whatsapp` (the WhatsApp
bridge service).

## Define an agent

In Python — flat keyword arguments over a canonical [config](configuration.md):

```python
from voiceagent import VoiceAgent, run

agent = VoiceAgent(
    name="mock-demo",
    llm="mock", stt="mock", tts="mock",     # swap for google / deepgram / cartesia
    system_prompt="You are a demo agent. Echo what the user says.",
)

if __name__ == "__main__":
    run(agent)      # argv: dev / start / console / download-files
```

…or in YAML (see [`examples/agents.yaml`](../examples/agents.yaml)), loaded via
`VOICEAGENT_AGENTS_FILE`:

```yaml
agents:
  - name: mock-demo
    mode: pipeline
    prompt: { text: "You are a demo agent. Echo what the user says." }
    llm: { provider: mock }
    stt: { provider: mock }
    tts: { provider: mock }
```

## Talk to it, no keys, no cluster (console)

Runs a local mic/speaker session against mock providers — no LiveKit, no
credits:

```bash
uv run python examples/agents/mock_demo.py console
```

## The zero-credit browser demo (full stack)

This is the end-to-end proof: browser → LiveKit → worker → pipeline → browser,
all on the local [compose stack](deploy-local.md), still $0 on the mock agent.

```bash
cp .env.example .env          # defaults are wired for local dev
docker compose -f deploy/compose/docker-compose.yml up --build
```

Then create a session and open the demo page:

```bash
curl -s -X POST http://localhost:8080/v1/sessions \
  -H "Authorization: Bearer devtoken" \
  -H "Content-Type: application/json" \
  -d '{"agent_id":"mock-demo"}'
# -> {"session_id":"...","room":"va-mock-demo-...","livekit_url":"ws://127.0.0.1:7880","token":"..."}
```

Open <http://localhost:8080/demo> (served by the API from
`examples/browser-demo/`), pick `mock-demo`, allow the mic, and speak — you hear
the mock agent reply. That round trip is the framework's [§29 gate](concepts.md).

See [channels-browser](channels-browser.md) for the session API in full.

## Go live with real providers

Swap `mock` for real providers and supply their keys in `.env` (see
[providers](providers.md) for the catalogue and required env vars):

```python
agent = VoiceAgent(
    name="sales-agent",
    llm="google", model="gemini-2.5-flash",
    stt="deepgram", tts="cartesia",
    system_prompt="You are a concise sales assistant for {company}.",
)
```

Realtime (one model does STT+LLM+TTS) is a config change, not an architecture
change:

```python
agent = VoiceAgent(name="sales-agent", mode="realtime",
                   llm="google", model="gemini-2.0-flash-live-001",
                   system_prompt="...")
```

## Next steps

- [concepts](concepts.md) — the mental model (layers, worker fleet, dispatch).
- [configuration](configuration.md) · [prompts](prompts.md) · [tools](tools.md) · [memory](memory.md)
- [providers](providers.md) · [adding-a-provider](adding-a-provider.md)
- Channels: [browser](channels-browser.md) · [sip](channels-sip.md) · [whatsapp](channels-whatsapp.md)
- Deploy: [local](deploy-local.md) · [kubernetes](deploy-k8s.md)
- [observability](observability.md) · [recording](recording.md) · [load-testing](load-testing.md)
