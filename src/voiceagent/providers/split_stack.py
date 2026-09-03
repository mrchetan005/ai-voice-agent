"""Split-stack proxy: Deepgram ASR -> agent/LLM -> Cartesia/ElevenLabs TTS."""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import AsyncIterator
from typing import Any

from ..base import BaseVoiceAgentProxy
from ..models import SessionState
from ._common import _record
from .asr import DeepgramClassicASR, DeepgramFluxASR
from .llm import OpenAICompatLLM, chunk_tokens
from .tts import CartesiaTTS, ElevenLabsTTS

logger = logging.getLogger("voiceagent")

# ---------------------------------------------------------------------------
# Split-stack proxy
# ---------------------------------------------------------------------------


class SplitStackProxy(BaseVoiceAgentProxy):
    """Deepgram ASR -> agent/LLM -> Cartesia/ElevenLabs TTS."""

    def __init__(self, config: Any, transport: Any) -> None:
        super().__init__(config, transport)
        opts = config.provider_options
        asr_kind = opts.get("asr", "deepgram")
        if asr_kind == "deepgram-flux":
            self.asr: DeepgramClassicASR | DeepgramFluxASR = DeepgramFluxASR(
                config.language, config.input_sample_rate, opts
            )
        else:
            self.asr = DeepgramClassicASR(config.language, config.input_sample_rate, opts)
        tts_kind = opts.get("tts", "cartesia")
        self.tts: CartesiaTTS | ElevenLabsTTS = (
            ElevenLabsTTS(config) if tts_kind == "elevenlabs" else CartesiaTTS(config)
        )
        self.llm = OpenAICompatLLM(opts)
        self._speech_started_at: float = 0.0
        self._turn_end_at: float = 0.0
        self._warm_task: asyncio.Task[int] | None = None

    async def _connect(self) -> None:
        started = time.monotonic()
        await asyncio.gather(self.asr.connect(), self.tts.open())
        _record(self, "handshake", (time.monotonic() - started) * 1000.0)
        # Warm the filler phrase cache in the background — never block the
        # session start on TTS round trips.
        self._warm_task = asyncio.create_task(
            self.phrase_cache.warm(
                self.tts.synthesize, self.config.language, self.config.voice_id
            )
        )

    async def _uplink_loop(self) -> None:
        async for frame in self.transport.recv_frames():
            await self.asr.send_audio(frame.data)  # binary passthrough, no copy
        await self.stop()

    async def _engine_loop(self) -> None:
        async for kind, payload in self.asr.events():
            if kind == "speech_started":
                self._speech_started_at = time.monotonic()
                await self.on_user_speech_started()
            elif kind == "final" and payload:
                self._turn_end_at = time.monotonic()
                if self._speech_started_at:
                    _record(self, "asr_latency",
                            (time.monotonic() - self._speech_started_at) * 1000.0)
                    self._speech_started_at = 0.0
                # TTFB≈0 acknowledgment from the local phrase cache while the
                # agent starts thinking (only when nothing is queued already).
                if self._bridge is not None and self._audio_out.qsize() == 0:
                    self._bridge.commentary.speak_cached_filler()
                await self.on_user_transcript(payload)

    async def speak_text(self, text: str, *, interrupt: bool = False) -> None:
        if interrupt:
            await self.tts.cancel()
            await self.interrupt_playback()
        # Exact-match phrase cache: stock lines skip TTS entirely.
        cached = self.phrase_cache.get(text, self.config.language, self.config.voice_id)
        if cached is not None:
            self.enqueue_audio(cached)
            return
        self.set_state(SessionState.SPEAKING)

        async def single() -> AsyncIterator[str]:
            yield text

        first_byte = time.monotonic()
        got_first = False

        def on_pcm(pcm: bytes) -> None:
            nonlocal got_first
            if not got_first:
                got_first = True
                _record(self, "tts_first_byte", (time.monotonic() - first_byte) * 1000.0)
            self.enqueue_audio(pcm)

        await self.tts.speak(single(), on_pcm)
        self.set_state(SessionState.LISTENING)

    async def speak_token_stream(self, chunks: AsyncIterator[str]) -> bool:
        """LLM tokens -> chunker -> TTS continuation context.

        This is the split-stack TTFB pipeline: ``chunk_tokens`` flushes the
        first ~4 words immediately; Cartesia's context stitches prosody
        across the following clause-sized pieces.
        """
        self.set_state(SessionState.SPEAKING)
        first_byte = time.monotonic()
        got_first = False

        def on_pcm(pcm: bytes) -> None:
            nonlocal got_first
            if not got_first:
                got_first = True
                now = time.monotonic()
                _record(self, "tts_first_byte", (now - first_byte) * 1000.0)
                # The caller-felt pause: end of their utterance -> first
                # reply audio (spans ASR close-out, LLM TTFB, chunking, TTS).
                if self._turn_end_at:
                    _record(self, "e2e_response", (now - self._turn_end_at) * 1000.0)
                    self._turn_end_at = 0.0
            self.enqueue_audio(pcm)

        await self.tts.speak(chunk_tokens(chunks), on_pcm)
        self.set_state(SessionState.LISTENING)
        return True

    @property
    def usage(self) -> dict[str, float]:
        """Neutral per-session usage counters for cost accounting."""
        return {
            "asr_audio_seconds": self.asr.audio_seconds_sent,
            "llm_input_tokens": self.llm.usage["input_tokens"],
            "llm_output_tokens": self.llm.usage["output_tokens"],
            "tts_characters": float(self.tts.characters_sent),
        }

    async def close(self) -> None:
        if self._warm_task is not None:
            self._warm_task.cancel()
        await self.asr.close()
        await self.tts.close()
        await self.llm.aclose()
        await self.transport.close()

