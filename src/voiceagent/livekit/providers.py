"""Built-in provider factories: registry names -> livekit plugin instances.

Self-hosted rule: always construct plugin classes with your own API keys —
never pass string model ids to AgentSession (those route to the LiveKit
Cloud inference gateway). Every factory lazy-imports its plugin so the
missing-extra error is clear and the core stays import-light.
"""

from __future__ import annotations

import logging
import os

from voiceagent.registry import Kind, ProviderRegistry, ProviderSpec
from voiceagent.registry import registry as default_registry

logger = logging.getLogger("voiceagent")

# Env var each built-in provider needs at runtime (mock needs none).
PROVIDER_KEY_ENVS: dict[tuple[str, str], str] = {
    ("llm", "google"): "GOOGLE_API_KEY",
    ("llm", "openai"): "OPENAI_API_KEY",
    ("stt", "deepgram"): "DEEPGRAM_API_KEY",
    ("tts", "cartesia"): "CARTESIA_API_KEY",
    ("tts", "elevenlabs"): "ELEVEN_API_KEY",
    ("realtime", "google"): "GOOGLE_API_KEY",
    ("realtime", "openai"): "OPENAI_API_KEY",
}


def missing_provider_keys(kind: Kind, name: str) -> list[str]:
    env = PROVIDER_KEY_ENVS.get((kind, name))
    return [env] if env and not os.environ.get(env) else []


def _with(spec: ProviderSpec, **fixed: object) -> dict[str, object]:
    """spec.options as kwargs, plus non-None fixed args (options win on clash)."""
    kwargs = {k: v for k, v in fixed.items() if v is not None}
    kwargs.update(spec.options)
    return kwargs


def _google_llm(spec: ProviderSpec):
    from livekit.plugins import google

    return google.LLM(**_with(spec, model=spec.model, temperature=spec.temperature))


def _openai_llm(spec: ProviderSpec):
    from livekit.plugins import openai

    return openai.LLM(**_with(spec, model=spec.model, temperature=spec.temperature))


def _mock_llm(spec: ProviderSpec):
    from voiceagent.livekit.mock import MockLLM

    return MockLLM(**spec.options)


def _deepgram_stt(spec: ProviderSpec):
    from livekit.plugins import deepgram

    language = spec.language
    if language == "en":  # deepgram expects a region tag for english
        language = "en-US"
    return deepgram.STT(**_with(spec, model=spec.model, language=language))


def _mock_stt(spec: ProviderSpec):
    from voiceagent.livekit.mock import MockSTT

    return MockSTT(**spec.options)


def _cartesia_tts(spec: ProviderSpec):
    from livekit.plugins import cartesia

    language = (spec.language or "en").split("-")[0]
    return cartesia.TTS(**_with(spec, model=spec.model, voice=spec.voice, language=language))


def _elevenlabs_tts(spec: ProviderSpec):
    from livekit.plugins import elevenlabs

    return elevenlabs.TTS(**_with(spec, model=spec.model, voice_id=spec.voice))


def _mock_tts(spec: ProviderSpec):
    from voiceagent.livekit.mock import MockTTS

    return MockTTS(**spec.options)


def _google_realtime(spec: ProviderSpec):
    from livekit.plugins import google

    return google.realtime.RealtimeModel(
        **_with(spec, model=spec.model, voice=spec.voice, temperature=spec.temperature)
    )


def _openai_realtime(spec: ProviderSpec):
    from livekit.plugins import openai

    return openai.realtime.RealtimeModel(**_with(spec, model=spec.model, voice=spec.voice))


def _silero_vad(spec: ProviderSpec):
    from livekit.plugins import silero

    return silero.VAD.load(**spec.options)


_BUILTINS: dict[tuple[Kind, str], object] = {
    ("llm", "google"): _google_llm,
    ("llm", "openai"): _openai_llm,
    ("llm", "mock"): _mock_llm,
    ("stt", "deepgram"): _deepgram_stt,
    ("stt", "mock"): _mock_stt,
    ("tts", "cartesia"): _cartesia_tts,
    ("tts", "elevenlabs"): _elevenlabs_tts,
    ("tts", "mock"): _mock_tts,
    ("realtime", "google"): _google_realtime,
    ("realtime", "openai"): _openai_realtime,
    ("vad", "silero"): _silero_vad,
}


def ensure_builtins(registry: ProviderRegistry | None = None) -> ProviderRegistry:
    """Register the built-in factories (idempotent)."""
    reg = registry or default_registry
    for (kind, name), factory in _BUILTINS.items():
        reg.register(kind, name, factory, overwrite=True)  # type: ignore[arg-type]
    return reg
