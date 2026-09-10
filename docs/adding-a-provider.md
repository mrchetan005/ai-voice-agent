# Adding a provider

Register a custom provider by writing a factory and adding it to the registry under a name — no core changes.

A provider is a `Factory`: a callable that takes a `ProviderSpec` and returns one plugin instance for a `kind` (`llm`, `stt`, `tts`, `realtime`, `vad`). The compile seam resolves providers by name from the module-level `registry`, so once yours is registered you reference it exactly like a built-in (see [providers](providers.md)).

## The pieces

```python
# voiceagent/registry.py
Kind = Literal["llm", "stt", "tts", "realtime", "vad"]

@dataclass(slots=True)
class ProviderSpec:
    model: str | None = None
    language: str = "en"
    voice: str | None = None
    temperature: float | None = None
    options: dict[str, Any] = field(default_factory=dict)

Factory = Callable[[ProviderSpec], Any]
```

`registry` is the module-level default `ProviderRegistry`; most apps use only that one.

## Register with a decorator

There is one decorator per kind: `register_llm`, `register_stt`, `register_tts`, `register_realtime`, `register_vad`. Each takes the provider name (and optional `overwrite`):

```python
from voiceagent.registry import register_tts, ProviderSpec

@register_tts("myttts")
def _my_tts(spec: ProviderSpec):
    from mypkg.livekit_plugin import MyTTS   # lazy-import the SDK
    return MyTTS(
        model=spec.model or "default-voice-model",
        voice=spec.voice,
        language=spec.language,
        **spec.options,                       # provider-specific plugin kwargs
    )
```

Read your API key from the environment inside the factory — never from `spec` or config:

```python
import os
return MyTTS(api_key=os.environ["MYTTS_API_KEY"], ...)
```

The return value must satisfy the LiveKit plugin interface for that kind (an `LLM`, `STT`, `TTS`, `realtime.RealtimeModel`, or `VAD`); it is handed straight to `AgentSession`.

## Register imperatively

Equivalent, without a decorator:

```python
from voiceagent.registry import registry

registry.register("tts", "myttts", _my_tts)
```

`register` raises `ValueError` if the `kind` is unknown, or if the name is already taken and you did not pass `overwrite=True`.

## Use it

Reference the name anywhere a provider name is accepted — sugar API or [config](configuration.md):

```python
agent = VoiceAgent(name="demo", llm="openai", tts="myttts", system_prompt="…")
```

```yaml
tts: { provider: myttts, model: default-voice-model, options: { rate: 1.0 } }
```

## Loading order

Registration is a side effect of importing your module, so it must run before an agent is compiled. Import it at process startup — e.g. in the module that defines your agents, or a package `__init__` that the worker loads.

The compile seam calls `ensure_builtins()` before every build, which registers the built-in factories with `overwrite=True`. This never clobbers a custom provider as long as its name differs from a built-in (`google`, `openai`, `mock`, `deepgram`, `cartesia`, `elevenlabs`, `silero`). To deliberately replace a built-in, register **after** builtins are ensured, or pass `overwrite=True` and re-register on each worker start.

## What is not covered

- **Key preflight** — `missing_provider_keys()` reads a fixed built-in table (`PROVIDER_KEY_ENVS`) and will not report a custom provider's env vars. Validate keys yourself at startup if you need that.
- **Unknown names** — resolving a name that was never registered raises `UnknownProviderError` (a `KeyError`) listing the registered names for that kind.

## Reference

| symbol | purpose |
|--------|---------|
| `ProviderSpec` | inputs a factory receives (`model`, `language`, `voice`, `temperature`, `options`) |
| `Factory` | `Callable[[ProviderSpec], Any]` |
| `register_llm` / `register_stt` / `register_tts` / `register_realtime` / `register_vad` | decorators; args `(name, *, overwrite=False)` |
| `registry` | module-level default `ProviderRegistry` |
| `registry.register(kind, name, factory, *, overwrite=False)` | imperative registration |
| `registry.create(kind, name, spec=None)` | build an instance (used by the compile seam) |
| `registry.names(kind)` | sorted registered names for a kind |
| `UnknownProviderError` | raised by `create` for an unregistered name |

## See also

- [providers](providers.md) — the built-in catalogue.
- [configuration](configuration.md) — where provider names live.
