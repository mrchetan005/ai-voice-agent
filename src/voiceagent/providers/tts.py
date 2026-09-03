"""Split-stack TTS engines: Cartesia (persistent WS, contexts) and ElevenLabs (stream-input)."""

from __future__ import annotations

import asyncio
import base64
import contextlib
import json
import logging
from collections.abc import AsyncIterator, Callable
from typing import Any

import websockets

from ._common import _require_env

logger = logging.getLogger("voiceagent")



# ---------------------------------------------------------------------------
# Split stack: TTS engines
# ---------------------------------------------------------------------------


class CartesiaTTS:
    """Cartesia WebSocket TTS with input continuations (contexts).

    One persistent socket; each utterance is a fresh ``context_id``.  Token
    chunks stream in with ``continue: true`` — Cartesia stitches prosody
    across chunks, which is why this is the preferred low-latency TTS.
    Barge-in is a first-class ``cancel`` message.
    """

    VERSION = "2026-08-14"
    DEFAULT_VOICE = "a0e99841-438c-4a64-b679-ae501e7d6091"

    def __init__(self, config: Any) -> None:
        self._config = config
        self._voice = config.voice_id or config.provider_options.get(
            "tts_voice", self.DEFAULT_VOICE
        )
        self._model = config.provider_options.get("tts_model", "sonic-latest")
        self._ws: Any = None
        self._context_seq = 0
        self._active_context: str | None = None
        self._lock = asyncio.Lock()  # one utterance in flight at a time
        self.characters_sent: int = 0

    async def open(self) -> None:
        self._ws = await websockets.connect(
            f"wss://api.cartesia.ai/tts/websocket?cartesia_version={self.VERSION}",
            additional_headers={"X-API-Key": _require_env("CARTESIA_API_KEY")},
        )

    def _payload(self, text: str, context_id: str, cont: bool) -> str:
        return json.dumps({
            "model_id": self._model,
            "transcript": text,
            "voice": self._voice,
            "language": self._config.language.split("-")[0],
            "context_id": context_id,
            "output_format": {
                "container": "raw",
                "encoding": "pcm_s16le",
                "sample_rate": self._config.output_sample_rate,
            },
            "continue": cont,
            # Default buffer delay is 3000 ms — far too conservative for a
            # live conversation; 500 ms trades a little prosody lookahead
            # for a much earlier first byte.
            "max_buffer_delay_ms": 500,
        })

    async def speak(
        self, chunks: AsyncIterator[str], on_pcm: Callable[[bytes], None]
    ) -> None:
        async with self._lock:
            self._context_seq += 1
            context_id = f"ctx-{self._context_seq}"
            self._active_context = context_id
            try:
                async with asyncio.TaskGroup() as tg:
                    tg.create_task(self._send_chunks(chunks, context_id))
                    tg.create_task(self._recv_audio(context_id, on_pcm))
            finally:
                self._active_context = None

    async def _send_chunks(self, chunks: AsyncIterator[str], context_id: str) -> None:
        async for piece in chunks:
            if self._active_context != context_id:
                return  # cancelled mid-stream
            self.characters_sent += len(piece)
            await self._ws.send(self._payload(piece, context_id, cont=True))
        if self._active_context == context_id:
            # Empty final chunk closes the context and flushes remaining audio.
            await self._ws.send(self._payload("", context_id, cont=False))

    async def _recv_audio(
        self, context_id: str, on_pcm: Callable[[bytes], None]
    ) -> None:
        async for raw in self._ws:
            msg = json.loads(raw)
            if msg.get("context_id") != context_id:
                continue  # audio for an already-cancelled context
            mtype = msg.get("type")
            if mtype == "chunk":
                on_pcm(base64.b64decode(msg["data"]))
            elif mtype == "done":
                return
            elif mtype == "error":
                logger.error("cartesia error: %s", msg)
                return

    async def synthesize(self, text: str) -> bytes:
        """One-shot synthesis (phrase-cache warming)."""
        parts: list[bytes] = []

        async def single() -> AsyncIterator[str]:
            yield text

        await self.speak(single(), parts.append)
        return b"".join(parts)

    async def cancel(self) -> None:
        if self._active_context is not None and self._ws is not None:
            ctx = self._active_context
            self._active_context = None  # stops sender + recv routing
            with contextlib.suppress(Exception):
                await self._ws.send(json.dumps({"context_id": ctx, "cancel": True}))

    async def close(self) -> None:
        if self._ws is not None:
            with contextlib.suppress(Exception):
                await self._ws.close()
            self._ws = None


class ElevenLabsTTS:
    """ElevenLabs ``stream-input`` WebSocket TTS (eleven_flash_v2_5).

    The stream-input socket is one-shot per utterance (EOS ends it), so we
    connect per ``speak``.  There is no cancel message on this API: barge-in
    closes the socket, which both stops synthesis and stops billing.
    """

    DEFAULT_VOICE = "21m00Tcm4TlvDq8ikWAM"

    def __init__(self, config: Any) -> None:
        self._config = config
        self._voice = config.voice_id or config.provider_options.get(
            "tts_voice", self.DEFAULT_VOICE
        )
        self._model = config.provider_options.get("tts_model", "eleven_flash_v2_5")
        self._active_ws: Any = None
        self._lock = asyncio.Lock()
        self.characters_sent: int = 0

    def _url(self) -> str:
        rate = self._config.output_sample_rate
        return (
            f"wss://api.elevenlabs.io/v1/text-to-speech/{self._voice}/stream-input"
            f"?model_id={self._model}&output_format=pcm_{rate}"
            f"&language_code={self._config.language.split('-')[0]}"
        )

    async def speak(
        self, chunks: AsyncIterator[str], on_pcm: Callable[[bytes], None]
    ) -> None:
        async with self._lock:
            ws = await websockets.connect(
                self._url(),
                additional_headers={"xi-api-key": _require_env("ELEVENLABS_API_KEY")},
            )
            self._active_ws = ws
            try:
                # BOS: text must be a single space. Small first value in the
                # chunk_length_schedule = lower first-audio latency.
                await ws.send(json.dumps({
                    "text": " ",
                    "voice_settings": {"stability": 0.5, "similarity_boost": 0.75},
                    "generation_config": {"chunk_length_schedule": [50, 120, 160, 290]},
                }))
                async with asyncio.TaskGroup() as tg:
                    tg.create_task(self._send_chunks(ws, chunks))
                    tg.create_task(self._recv_audio(ws, on_pcm))
            except* websockets.ConnectionClosed:
                pass  # cancel() closed the socket mid-utterance
            finally:
                self._active_ws = None
                with contextlib.suppress(Exception):
                    await ws.close()

    async def _send_chunks(self, ws: Any, chunks: AsyncIterator[str]) -> None:
        async for piece in chunks:
            self.characters_sent += len(piece)
            # Trailing space is required for correct word joining.
            await ws.send(json.dumps({"text": piece.rstrip() + " ", "flush": False}))
        await ws.send(json.dumps({"text": " ", "flush": True}))
        await ws.send(json.dumps({"text": ""}))  # EOS

    @staticmethod
    async def _recv_audio(ws: Any, on_pcm: Callable[[bytes], None]) -> None:
        async for raw in ws:
            msg = json.loads(raw)
            if msg.get("audio"):
                on_pcm(base64.b64decode(msg["audio"]))
            if msg.get("isFinal"):
                return

    async def synthesize(self, text: str) -> bytes:
        parts: list[bytes] = []

        async def single() -> AsyncIterator[str]:
            yield text

        await self.speak(single(), parts.append)
        return b"".join(parts)

    async def cancel(self) -> None:
        if self._active_ws is not None:
            with contextlib.suppress(Exception):
                await self._active_ws.close()

    async def close(self) -> None:
        await self.cancel()

    async def open(self) -> None:  # parity with CartesiaTTS; nothing to pre-open
        return None

