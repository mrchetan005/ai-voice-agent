"""Offline mock providers: prove the full media path with zero credits.

MockSTT returns a fixed transcript when VAD detects end of speech, MockLLM
echoes the user, MockTTS synthesizes a sine tone — so a browser session
against the mock agent exercises browser -> LiveKit -> worker -> pipeline ->
browser end to end without a single provider API call.
"""

from __future__ import annotations

import array
import math
import uuid

from livekit.agents import llm, stt, tts
from livekit.agents.types import (
    DEFAULT_API_CONNECT_OPTIONS,
    NOT_GIVEN,
    APIConnectOptions,
)
from livekit.agents.utils import AudioBuffer


class MockSTT(stt.STT):
    """Non-streaming STT; AgentSession wraps it with VAD-driven chunking."""

    def __init__(self, *, transcript: str = "Hello agent, I am testing the voice pipeline.") -> None:
        super().__init__(
            capabilities=stt.STTCapabilities(streaming=False, interim_results=False)
        )
        self._transcript = transcript

    @property
    def provider(self) -> str:
        return "mock"

    @property
    def model(self) -> str:
        return "mock"

    async def _recognize_impl(
        self,
        buffer: AudioBuffer,
        *,
        language=NOT_GIVEN,
        conn_options: APIConnectOptions,
    ) -> stt.SpeechEvent:
        return stt.SpeechEvent(
            type=stt.SpeechEventType.FINAL_TRANSCRIPT,
            request_id=f"mock-{uuid.uuid4().hex[:8]}",
            alternatives=[stt.SpeechData(language="en", text=self._transcript)],
        )


class _MockLLMStream(llm.LLMStream):
    def __init__(self, mock_llm: MockLLM, *, chat_ctx, tools, conn_options, reply: str | None):
        super().__init__(mock_llm, chat_ctx=chat_ctx, tools=tools, conn_options=conn_options)
        self._reply = reply

    def _last_user_text(self) -> str:
        for item in reversed(self._chat_ctx.items):
            if getattr(item, "type", "") == "message" and getattr(item, "role", "") == "user":
                return item.text_content or ""
        return ""

    async def _run(self) -> None:
        text = self._reply or f"You said: {self._last_user_text() or 'nothing yet'}."
        request_id = f"mock-{uuid.uuid4().hex[:8]}"
        words = text.split(" ")
        for i, word in enumerate(words):
            chunk = word if i == len(words) - 1 else word + " "
            self._event_ch.send_nowait(
                llm.ChatChunk(
                    id=request_id,
                    delta=llm.ChoiceDelta(role="assistant", content=chunk),
                )
            )
        self._event_ch.send_nowait(
            llm.ChatChunk(
                id=request_id,
                usage=llm.CompletionUsage(
                    completion_tokens=len(words),
                    prompt_tokens=len(words),
                    total_tokens=2 * len(words),
                ),
            )
        )


class MockLLM(llm.LLM):
    """Echoes the last user message (or a fixed reply if provided)."""

    def __init__(self, *, reply: str | None = None) -> None:
        super().__init__()
        self._reply = reply

    @property
    def provider(self) -> str:
        return "mock"

    @property
    def model(self) -> str:
        return "mock"

    def chat(
        self,
        *,
        chat_ctx: llm.ChatContext,
        tools=None,
        conn_options: APIConnectOptions = DEFAULT_API_CONNECT_OPTIONS,
        parallel_tool_calls=NOT_GIVEN,
        tool_choice=NOT_GIVEN,
        extra_kwargs=NOT_GIVEN,
    ) -> llm.LLMStream:
        return _MockLLMStream(
            self, chat_ctx=chat_ctx, tools=tools or [], conn_options=conn_options, reply=self._reply
        )


class _MockChunkedStream(tts.ChunkedStream):
    async def _run(self, output_emitter: tts.AudioEmitter) -> None:
        sample_rate = self._tts.sample_rate
        output_emitter.initialize(
            request_id=f"mock-{uuid.uuid4().hex[:8]}",
            sample_rate=sample_rate,
            num_channels=1,
            mime_type="audio/pcm",
        )
        text = self.input_text
        duration_s = min(0.3 + 0.03 * len(text), 2.5)
        tone_hz, amplitude = 440.0, 6000
        samples = int(duration_s * sample_rate)
        pcm = array.array(
            "h",
            (
                int(amplitude * math.sin(2.0 * math.pi * tone_hz * i / sample_rate))
                for i in range(samples)
            ),
        )
        output_emitter.push(pcm.tobytes())
        output_emitter.flush()


class MockTTS(tts.TTS):
    """Synthesizes a sine tone whose length tracks the text length."""

    def __init__(self, *, sample_rate: int = 16000) -> None:
        super().__init__(
            capabilities=tts.TTSCapabilities(streaming=False),
            sample_rate=sample_rate,
            num_channels=1,
        )

    @property
    def provider(self) -> str:
        return "mock"

    @property
    def model(self) -> str:
        return "mock"

    def synthesize(
        self, text: str, *, conn_options: APIConnectOptions = DEFAULT_API_CONNECT_OPTIONS
    ) -> tts.ChunkedStream:
        return _MockChunkedStream(tts=self, input_text=text, conn_options=conn_options)
