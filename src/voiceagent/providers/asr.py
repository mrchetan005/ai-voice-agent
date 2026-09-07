"""Split-stack ASR: Deepgram classic (/v1/listen) and Flux (/v2/listen) streaming clients."""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from collections.abc import AsyncIterator
from typing import Any

import websockets

from ._common import _require_env

logger = logging.getLogger("voiceagent")


async def _open_deepgram_ws(url: str) -> Any:
    """Open a Deepgram streaming socket with a generous handshake timeout
    and one retry.

    The handshake shares the event loop with the rest of call setup (aioice
    ICE, the ~7 s dual-brain greet). The websockets default open_timeout
    (~10 s) is too tight when the loop is briefly contended at session
    start — it surfaced as ``TimeoutError: timed out during opening
    handshake`` even though the raw socket opens in well under a second."""
    headers = {"Authorization": f"Token {_require_env('DEEPGRAM_API_KEY')}"}
    last: Exception | None = None
    for attempt in (1, 2):
        try:
            return await websockets.connect(
                url, additional_headers=headers, open_timeout=20
            )
        except Exception as exc:  # retried below, then re-raised
            last = exc
            logger.warning(
                "Deepgram ASR connect attempt %d/2 failed: %r", attempt, exc
            )
            if attempt == 1:
                await asyncio.sleep(0.5)
    raise last  # type: ignore[misc]


# ---------------------------------------------------------------------------
# Split stack: Deepgram ASR
# ---------------------------------------------------------------------------


class DeepgramClassicASR:
    """``/v1/listen`` streaming client (nova-3). Broad language coverage."""

    def __init__(self, language: str, sample_rate: int, options: dict[str, Any]) -> None:
        lang = options.get("asr_language") or language.split("-")[0]
        params = {
            "model": options.get("asr_model", "nova-3"),
            "language": lang,
            "encoding": "linear16",
            "sample_rate": str(sample_rate),
            "interim_results": "true",
            "endpointing": str(options.get("endpointing_ms", 100)),
            # Without this param Deepgram never emits UtteranceEnd, leaving
            # the noisy-line fallback in events() dead.
            "utterance_end_ms": str(options.get("utterance_end_ms", 1000)),
            "vad_events": "true",
            "smart_format": "true",
        }
        query = "&".join(f"{k}={v}" for k, v in params.items())
        self._url = f"wss://api.deepgram.com/v1/listen?{query}"
        self._ws: Any = None
        self._keepalive: asyncio.Task[None] | None = None
        self._sample_rate = sample_rate
        self.audio_seconds_sent: float = 0.0

    async def connect(self) -> None:
        self._ws = await _open_deepgram_ws(self._url)
        # Deepgram closes idle sockets (~10 s, NET-0001); KeepAlive every 5 s
        # is harmless during active audio and mandatory during user silence.
        self._keepalive = asyncio.create_task(self._keepalive_loop())

    async def _keepalive_loop(self) -> None:
        while True:
            await asyncio.sleep(5.0)
            with contextlib.suppress(Exception):
                await self._ws.send(json.dumps({"type": "KeepAlive"}))

    async def send_audio(self, pcm: bytes) -> None:
        self.audio_seconds_sent += len(pcm) / (self._sample_rate * 2)
        await self._ws.send(pcm)  # raw binary in — zero re-encoding

    async def events(self) -> AsyncIterator[tuple[str, str | None]]:
        """Yields ``("speech_started", None)`` and ``("final", transcript)``."""
        pending: list[str] = []
        async for raw in self._ws:
            if isinstance(raw, bytes):
                continue
            msg = json.loads(raw)
            mtype = msg.get("type")
            if mtype == "SpeechStarted":
                yield ("speech_started", None)
            elif mtype == "Results":
                alt = (msg.get("channel", {}).get("alternatives") or [{}])[0]
                text = alt.get("transcript", "")
                if not text:
                    continue
                if msg.get("is_final"):
                    pending.append(text)
                    # speech_final = endpointing fired: the utterance is done.
                    if msg.get("speech_final"):
                        yield ("final", " ".join(pending))
                        pending.clear()
            elif mtype == "UtteranceEnd" and pending:
                # Fallback close-out when speech_final never fired (noise).
                yield ("final", " ".join(pending))
                pending.clear()

    async def close(self) -> None:
        if self._keepalive is not None:
            self._keepalive.cancel()
        if self._ws is not None:
            with contextlib.suppress(Exception):
                await self._ws.send(json.dumps({"type": "CloseStream"}))
                await self._ws.close()


class DeepgramFluxASR:
    """``/v2/listen`` Flux client — model-integrated turn detection (~260 ms).

    English (``flux-general-en``) or 10-language ``flux-general-multi``.
    Preferred for agents; fall back to :class:`DeepgramClassicASR` for
    languages Flux doesn't cover.
    """

    def __init__(self, language: str, sample_rate: int, options: dict[str, Any]) -> None:
        lang = language.split("-")[0]
        model = options.get("asr_model") or (
            "flux-general-en" if lang == "en" else "flux-general-multi"
        )
        params = {
            "model": model,
            "encoding": "linear16",
            "sample_rate": str(sample_rate),
            "eot_threshold": str(options.get("eot_threshold", 0.7)),
        }
        query = "&".join(f"{k}={v}" for k, v in params.items())
        if model.endswith("multi") and lang != "en":
            query += f"&language_hint={lang}"
        self._url = f"wss://api.deepgram.com/v2/listen?{query}"
        self._ws: Any = None
        self._sample_rate = sample_rate
        self.audio_seconds_sent: float = 0.0

    async def connect(self) -> None:
        self._ws = await _open_deepgram_ws(self._url)

    async def send_audio(self, pcm: bytes) -> None:
        self.audio_seconds_sent += len(pcm) / (self._sample_rate * 2)
        await self._ws.send(pcm)

    async def events(self) -> AsyncIterator[tuple[str, str | None]]:
        turn_active = False
        async for raw in self._ws:
            if isinstance(raw, bytes):
                continue
            msg = json.loads(raw)
            if msg.get("type") != "TurnInfo":
                continue
            event = msg.get("event")
            if event == "EndOfTurn":
                turn_active = False
                if text := msg.get("transcript"):
                    yield ("final", text)
            elif event == "TurnResumed":
                continue  # speculative EOT withdrawn; keep listening
            else:
                # Any mid-turn update doubles as our speech-start signal.
                if not turn_active and msg.get("transcript"):
                    turn_active = True
                    yield ("speech_started", None)

    async def close(self) -> None:
        if self._ws is not None:
            with contextlib.suppress(Exception):
                await self._ws.close()

