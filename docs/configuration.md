# Configuration

The declarative shape of an agent (`AgentConfig` + YAML) and the platform's environment settings.

Everything the [quickstart](quickstart.md) `VoiceAgent(...)` sugar accepts compiles down to an
`AgentConfig`. YAML and dict definitions land on the same model, validated by pydantic.

## AgentConfig

```python
from voiceagent import AgentConfig

cfg = AgentConfig.from_yaml("examples/agents.yaml")   # single-agent file
cfg = AgentConfig.from_dict({"name": "demo", "mode": "pipeline", ...})
```

| Field | Type | Default | Notes |
| --- | --- | --- | --- |
| `name` | `str` | required | lowercase letters/digits/hyphens (`^[a-z0-9][a-z0-9-]*$`); doubles as the LiveKit `agent_name` and appears in room names |
| `mode` | `"pipeline"` \| `"realtime"` | `"pipeline"` | selects STT→LLM→TTS vs. a single realtime model |
| `prompt` | `PromptConfig` | required | see [prompts](prompts.md) |
| `llm` | `LLMConfig` \| `None` | `None` | required in pipeline mode |
| `stt` | `STTConfig` \| `None` | `None` | required in pipeline mode |
| `tts` | `TTSConfig` \| `None` | `None` | required in pipeline mode |
| `realtime` | `RealtimeConfig` \| `None` | `None` | required in realtime mode |
| `tools` | `list[str]` | `[]` | dotted paths `"pkg.module:tool_obj"`; see [tools](tools.md) |
| `language` | `str` | `"en"` | default for STT/TTS/realtime when they don't set their own |
| `greeting` | `str` \| `None` | `None` | spoken opener when the session starts |
| `memory` | `MemoryConfig` \| `None` | `None` | see [memory](memory.md) |
| `turn_detection` | `"vad"` \| `"multilingual"` | `"vad"` | `multilingual` loads the turn-detector model |
| `allow_interruptions` | `bool` | `True` | let the caller barge in |
| `max_tool_steps` | `int` | `5` | max tool calls the LLM may chain per turn |
| `metadata` | `dict[str, str]` | `{}` | free-form labels |

### Mode consistency (validated)

- **pipeline** requires `llm`, `stt`, and `tts`; rejects `realtime`.
- **realtime** requires `realtime`; rejects `llm`/`stt`/`tts`.

Violations raise `ValueError` at construction.

### Provider sub-configs

`LLMConfig`, `STTConfig`, `TTSConfig`, `RealtimeConfig`, and `MemoryConfig` are the building blocks.
`provider` is a name resolved by the [provider registry](providers.md); `options` is passed through to the
plugin factory.

| Config | Field | Type | Default |
| --- | --- | --- | --- |
| `LLMConfig` | `provider` | `str` | required |
| | `model` | `str` \| `None` | `None` |
| | `temperature` | `float` \| `None` | `None` |
| | `options` | `dict` | `{}` |
| `STTConfig` | `provider` | `str` | `"deepgram"` |
| | `model` | `str` \| `None` | `None` |
| | `language` | `str` | `"en"` |
| | `options` | `dict` | `{}` |
| `TTSConfig` | `provider` | `str` | `"cartesia"` |
| | `model` | `str` \| `None` | `None` |
| | `voice` | `str` \| `None` | `None` |
| | `language` | `str` | `"en"` |
| | `options` | `dict` | `{}` |
| `RealtimeConfig` | `provider` | `str` | required |
| | `model` | `str` \| `None` | `None` |
| | `voice` | `str` \| `None` | `None` |
| | `options` | `dict` | `{}` |
| `MemoryConfig` | `backend` | `"inmemory"` \| `"redis"` \| `"postgres"` | `"inmemory"` |
| | `url` | `str` \| `None` | `None` (falls back to platform settings) |
| | `max_messages` | `int` | `50` |
| | `ttl_s` | `int` | `604_800` (7 days) |

## YAML format

A YAML file is either a single agent mapping or `{"agents": [ ... ]}`. Load one file of many agents with
`load_agents_yaml()` (duplicate `name`s are rejected):

```python
from voiceagent import load_agents_yaml, VoiceAgent

agents = [VoiceAgent.from_config(c) for c in load_agents_yaml("examples/agents.yaml")]
```

Canonical example (`examples/agents.yaml`):

```yaml
agents:
  - name: sales-pipeline
    mode: pipeline
    prompt:
      text: |
        You are a concise, friendly sales assistant for {company}.
        Answer in at most two short sentences — this is a voice call.
      variables:
        company: Acme Corp
    llm: { provider: google, model: gemini-2.5-flash, temperature: 0.4 }
    stt: { provider: deepgram, model: nova-3, language: en }
    tts: { provider: cartesia }
    tools:
      - examples.tools_demo:get_available_slots
      - examples.tools_demo:book_appointment
    greeting: Greet the caller briefly and ask how you can help.
    memory:
      backend: redis
    turn_detection: multilingual
```

`AgentConfig.from_yaml(path)` loads a **single**-agent file; it raises if the file has an `agents:` key
(use `load_agents_yaml()` for those).

## Environment settings

Settings are env-only (secrets never live in YAML). Each group reads a prefix from the environment and an
optional `.env` file. `load_settings(require_livekit=False)` builds every group and reports **all** invalid
or missing variables in one `SettingsError` instead of failing one at a time. See [`.env.example`](../.env.example)
for the full contract.

| Group (prefix) | Variable | Default |
| --- | --- | --- |
| **LiveKit** `LIVEKIT_` | `LIVEKIT_URL` | `ws://localhost:7880` |
| | `LIVEKIT_PUBLIC_URL` | `""` (empty = same as `LIVEKIT_URL`) |
| | `LIVEKIT_API_KEY` | `""` (required when `require_livekit=True`) |
| | `LIVEKIT_API_SECRET` | `""` (required when `require_livekit=True`) |
| **Platform** `PLATFORM_` | `PLATFORM_API_TOKEN` | `""` |
| | `PLATFORM_PORT` | `8080` |
| | `PLATFORM_DATABASE_URL` | `""` |
| | `PLATFORM_REDIS_URL` | `""` |
| | `PLATFORM_SIP_TRUNK_ID` | `""` (default outbound trunk for `/v1/calls/sip`) |
| | `PLATFORM_DEMO_DIR` | `examples/browser-demo` (served at `/demo` when present) |
| **Worker** `WORKER_` | `WORKER_PROMETHEUS_PORT` | `9100` |
| | `WORKER_IDLE_PROCESSES` | `2` |
| | `WORKER_LOAD_THRESHOLD` | `0.7` |
| | `WORKER_DRAIN_TIMEOUT_S` | `600` |
| **Agent source** `VOICEAGENT_` | `VOICEAGENT_AGENTS` | `""` (comma-separated `pkg.module:attr` paths) |
| | `VOICEAGENT_AGENTS_FILE` | `""` (path to a YAML agents file) |
| | `VOICEAGENT_WORKER_NAME` | `voiceagent` (LiveKit `agent_name` the fleet registers under) |
| **Observability** (no prefix) | `OTEL_EXPORTER_OTLP_ENDPOINT` | `""` (empty = tracing off) |
| | `OTEL_EXPORTER_OTLP_PROTOCOL` | `http/protobuf` (or `grpc`) |
| | `OTEL_SERVICE_NAME` | `voiceagent` |
| | `METRICS_ENABLED` | `true` |
| **Recording** `RECORDING_` | `RECORDING_ENABLED` | `false` |
| | `RECORDING_S3_ENDPOINT` | `""` |
| | `RECORDING_BUCKET` | `""` (required when enabled) |
| | `RECORDING_ACCESS_KEY` | `""` (required when enabled) |
| | `RECORDING_SECRET_KEY` | `""` (required when enabled) |
| | `RECORDING_REGION` | `""` |
| | `RECORDING_PREFIX` | `recordings` |
| **WhatsApp** `WHATSAPP_` | `WHATSAPP_PHONE_NUMBER_ID` | `""` (required by the whatsapp service) |
| | `WHATSAPP_ACCESS_TOKEN` | `""` (required by the whatsapp service) |
| | `WHATSAPP_VERIFY_TOKEN` | `""` (webhook GET-verify handshake) |
| | `WHATSAPP_APP_SECRET` | `""` (validates `X-Hub-Signature-256`, optional) |
| | `WHATSAPP_GRAPH_VERSION` | `v24.0` |
| | `WHATSAPP_PORT` | `8090` |
| | `WHATSAPP_DEFAULT_AGENT` | `""` (agent_id serving inbound calls) |

Notes:

- One worker fleet serves **all** loaded agents; dispatch metadata (`agent_id`) selects which one runs per
  session. Run isolated fleets by giving them different `VOICEAGENT_WORKER_NAME`s.
- Provider API keys (`DEEPGRAM_API_KEY`, `CARTESIA_API_KEY`, `GOOGLE_API_KEY`, …) are read by the provider
  plugins, not by these settings groups — see [providers](providers.md).
- `PRICE_<PROVIDER>_<UNIT>` cost overrides feed usage accounting ([observability](observability.md)). Only the
  standalone `voiceagent-whatsapp` service requires the `WHATSAPP_` group; the worker and API never do.
