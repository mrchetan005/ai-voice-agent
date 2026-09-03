"""Concrete provider engines.

Three architectures behind one contract (:class:`BaseVoiceAgentProxy`):

* ``OpenAIRealtimeProxy`` — native omni engine in STRICT PROXY mode.
* ``GeminiLiveProxy`` — native omni engine in MODEL-FRONTED mode (native
  tools or the mandatory ``send_to_agent`` bridge).
* ``SplitStackProxy`` — Deepgram ASR + any OpenAI-compatible LLM + Cartesia
  or ElevenLabs TTS, with token-level chunking into TTS continuations.

This package split preserves the original ``voiceagent.providers`` public
API — every name importable before the split is importable here.
"""

from ._common import _record, _require_env  # noqa: F401  (internal, kept importable)
from .asr import DeepgramClassicASR, DeepgramFluxASR
from .gemini_live import GeminiLiveProxy
from .llm import OpenAICompatLLM, chunk_tokens, make_llm_agent
from .mock import MockProxy, synth_tone
from .openai_realtime import OpenAIRealtimeProxy
from .split_stack import SplitStackProxy
from .tts import CartesiaTTS, ElevenLabsTTS

__all__ = [
    "CartesiaTTS", "DeepgramClassicASR", "DeepgramFluxASR", "ElevenLabsTTS",
    "GeminiLiveProxy", "MockProxy", "OpenAICompatLLM", "OpenAIRealtimeProxy",
    "SplitStackProxy", "chunk_tokens", "make_llm_agent", "synth_tone",
]
