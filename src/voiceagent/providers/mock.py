"""Mock engine for the offline quickstart and eval harness."""

from __future__ import annotations

import asyncio
import logging
import math
import struct
from typing import Any

from ..base import BaseVoiceAgentProxy, EndOfStream
from ..models import SessionState
from ._common import _record

logger = logging.getLogger("voiceagent")



# ---------------------------------------------------------------------------
# Mock engine (offline quickstart + eval harness)
# ---------------------------------------------------------------------------


def synth_tone(duration_ms: int, sample_rate: int = 24_000, freq: float = 440.0) -> bytes:
    """Deterministic PCM16 sine burst standing in for synthesized speech."""
    n = int(sample_rate * duration_ms / 1000)
    amp = 12_000
    return struct.pack(
        f"<{n}h",
        *(int(amp * math.sin(2 * math.pi * freq * i / sample_rate)) for i in range(n)),
    )


class MockProxy(BaseVoiceAgentProxy):
    """No-network engine: scripted transcripts in, fake PCM + a spoken-text
    log out.  Powers the eval harness and ``--mock`` quickstart."""

    def __init__(self, config: Any, transport: Any) -> None:
        super().__init__(config, transport)
        self.script_queue: asyncio.Queue[str | EndOfStream] = asyncio.Queue()
        self.spoken: list[str] = []
        self.frames_received = 0

    async def _connect(self) -> None:
        _record(self, "handshake", 1.0)

    async def _uplink_loop(self) -> None:
        async for _frame in self.transport.recv_frames():
            self.frames_received += 1
        await self.stop()

    async def _engine_loop(self) -> None:
        while True:
            item = await self.script_queue.get()
            if isinstance(item, EndOfStream):
                await self.stop()
                return
            # Scripted "ASR": pretend detection + transcription took 40 ms.
            _record(self, "asr_latency", 40.0)
            await self.on_user_speech_started()
            await self.on_user_transcript(item)

    def push_user_utterance(self, text: str) -> None:
        self.script_queue.put_nowait(text)

    def end_script(self) -> None:
        self.script_queue.put_nowait(EndOfStream())

    async def speak_text(self, text: str, *, interrupt: bool = False) -> None:
        if interrupt:
            await self.interrupt_playback()
        self.spoken.append(text)
        self.set_state(SessionState.SPEAKING)
        _record(self, "tts_first_byte", 5.0)
        # ~60 ms of audio per word keeps mock timing roughly speech-shaped.
        self.enqueue_audio(
            synth_tone(min(60 * max(1, len(text.split())), 1500),
                       self.config.output_sample_rate)
        )
        self.set_state(SessionState.LISTENING)

    async def close(self) -> None:
        await self.transport.close()
