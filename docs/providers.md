# Providers

The built-in provider catalogue, how to select a provider, and the API-key env vars each one needs.

A provider is a named factory that builds one LiveKit plugin instance for a given kind: `llm`, `stt`, `tts`, `realtime`, or `vad`. You pick providers by name; the compile seam looks them up in the [registry](adding-a-provider.md) and constructs the plugin with your own API keys (self-hosted rule — string model ids are never passed to `AgentSession`).

## Catalogue

The framework pins **no default model**: `model` is only sent to the plugin when you set it, otherwise the plugin's own default applies. Secrets are read from the environment, never from config or `options`.

| kind | provider | default model | required env var(s) |
|------|----------|---------------|---------------------|
| llm | `google` | — (plugin default) | `GOOGLE_API_KEY` |
| llm | `openai` | — (plugin default) | `OPENAI_API_KEY` |
| llm | `mock` | — | (none) |
| stt | `deepgram` | — (plugin default) | `DEEPGRAM_API_KEY` |
| stt | `mock` | — | (none) |
| tts | `cartesia` | — (plugin default) | `CARTESIA_API_KEY` |
| tts | `elevenlabs` | — (plugin default) | `ELEVEN_API_KEY` |
| tts | `mock` | — | (none) |
| realtime | `google` | — (plugin default) | `GOOGLE_API_KEY` |
| realtime | `openai` | — (plugin default) | `OPENAI_API_KEY` |
| vad | `silero` | bundled model | (none) |

`mock` providers exist for the offline test suite and need no keys. `silero` VAD loads a bundled model — no network, no key.

## Selecting providers

### Sugar API

`VoiceAgent` takes provider names as flat keyword arguments (see [quickstart](quickstart.md)):

```python
from voiceagent import VoiceAgent

agent = VoiceAgent(
    name="sales-agent",
    llm="google", model="gemini-2.5-flash", temperature=0.4,
    stt="deepgram:nova-3",          # "provider:model" shorthand for stt/tts
    tts="cartesia", voice="…voice-id…",
    system_prompt="You are a concise assistant for {company}.",
    language="en",
)
```

- `llm` takes a bare provider name; its model comes from the separate `model=` (and `temperature=`) argument.
- `stt` and `tts` accept either a bare name (`"deepgram"`) or `"provider:model"` (`"deepgram:nova-3"`); when omitted they default to `stt="deepgram"` and `tts="cartesia"`.
- `voice=` sets the TTS voice; `llm_options=`, `stt_options=`, `tts_options=` pass provider-specific plugin kwargs.

Realtime mode uses one model as the whole voice — no `stt`/`tts`:

```python
agent = VoiceAgent(
    name="sales-agent", mode="realtime",
    llm="google", model="gemini-2.0-flash-live-001", voice="Puck",
    system_prompt="…",
)
```

### YAML / config

Each provider is a block in the [configuration](configuration.md) with `provider`, optional `model`, and an `options` dict:

```yaml
llm:  { provider: openai, model: gpt-4o-mini, temperature: 0.3 }
stt:  { provider: deepgram, model: nova-3, language: en }
tts:  { provider: cartesia, voice: "…voice-id…", language: en }
```

## How a spec becomes a plugin

At compile time each config block is turned into a `ProviderSpec` (`model`, `language`, `voice`, `temperature`, `options`) and passed to the provider factory. The factory merges the spec into the plugin constructor, and `options` wins on any key clash:

```python
# voiceagent/livekit/providers.py
def _openai_llm(spec):
    from livekit.plugins import openai
    return openai.LLM(**_with(spec, model=spec.model, temperature=spec.temperature))
```

Provider-specific handling to know about:

- **deepgram** — a `language` of `"en"` is rewritten to `"en-US"` (Deepgram wants a region tag).
- **cartesia** — `language` is truncated to its primary subtag (`"en-US"` → `"en"`).
- **elevenlabs** — the `voice` field maps to the plugin's `voice_id`; `language` is not passed.
- **silero** — built only from `options` (via `silero.VAD.load(**options)`); pipeline mode always uses `silero` for VAD, and there is no config block for it. To swap VAD, inject a prewarmed instance into the compile step or register a replacement (see [adding-a-provider](adding-a-provider.md)).

## Missing-key preflight

`missing_provider_keys(kind, name)` returns the env vars a **built-in** provider needs but that are unset — useful before starting a worker:

```python
from voiceagent.livekit.providers import missing_provider_keys

missing_provider_keys("tts", "elevenlabs")   # -> ["ELEVEN_API_KEY"] if unset, else []
```

It only knows the built-in table; custom providers are not covered.

## See also

- [adding-a-provider](adding-a-provider.md) — register your own provider.
- [configuration](configuration.md) — full config schema.
- [quickstart](quickstart.md) — end-to-end setup.
