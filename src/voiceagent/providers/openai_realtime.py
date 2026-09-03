"""OpenAI Realtime engine (strict proxy mode: the model only speaks what we tell it)."""

from __future__ import annotations

import base64
import contextlib
import json
import logging
import time
from typing import Any

import websockets

from ..base import BaseVoiceAgentProxy
from ..models import SessionState
from ._common import _record, _require_env

logger = logging.getLogger("voiceagent")



# ---------------------------------------------------------------------------
# OpenAI Realtime (strict proxy mode)
# ---------------------------------------------------------------------------


class OpenAIRealtimeProxy(BaseVoiceAgentProxy):
    """OpenAI Realtime over WebSocket, GA protocol.

    The wire is 24 kHz PCM16 only, so the facade forces
    ``input_sample_rate = output_sample_rate = 24000`` for this provider.
    """

    URL = "wss://api.openai.com/v1/realtime"

    def __init__(self, config: Any, transport: Any) -> None:
        super().__init__(config, transport)
        opts = config.provider_options
        self._model: str = opts.get("model", "gpt-realtime-2.1")
        self._voice: str = config.voice_id or opts.get("voice", "marin")
        self._ws: Any = None
        self._active_response_id: str | None = None
        self._response_started_at: float = 0.0
        # Neutral per-session usage counters (response.done carries usage).
        self.usage: dict[str, float] = {
            "input_tokens": 0.0, "input_tokens_text": 0.0,
            "input_tokens_audio": 0.0, "output_tokens": 0.0,
            "output_tokens_text": 0.0, "output_tokens_audio": 0.0,
            "audio_in_seconds": 0.0, "audio_out_seconds": 0.0, "turns": 0.0,
        }

    async def _connect(self) -> None:
        started = time.monotonic()
        self._ws = await websockets.connect(
            f"{self.URL}?model={self._model}",
            additional_headers={"Authorization": f"Bearer {_require_env('OPENAI_API_KEY')}"},
            max_size=16 * 1024 * 1024,  # audio deltas can be large
        )
        # GA session shape: session.type is required; audio config is nested.
        # create_response=False + interrupt_response=True = strict proxy: the
        # server VAD still segments turns and transcribes, but the model only
        # ever speaks when WE create a response.
        await self._ws.send(json.dumps({
            "type": "session.update",
            "session": {
                "type": "realtime",
                "model": self._model,
                "instructions": (
                    f"{self.config.system_prompt}\n"
                    f"Always speak in {self.config.language} with a "
                    f"{self.config.tone} tone."
                ),
                "output_modalities": ["audio"],
                "audio": {
                    "input": {
                        "format": {"type": "audio/pcm", "rate": 24000},
                        "transcription": {
                            "model": "gpt-4o-mini-transcribe",
                            "language": self.config.language.split("-")[0],
                        },
                        "turn_detection": {
                            "type": self.config.provider_options.get(
                                "turn_detection", "semantic_vad"
                            ),
                            "create_response": False,
                            "interrupt_response": True,
                        },
                    },
                    "output": {
                        "format": {"type": "audio/pcm", "rate": 24000},
                        "voice": self._voice,
                    },
                },
            },
        }))
        _record(self, "handshake", (time.monotonic() - started) * 1000.0)

    async def _uplink_loop(self) -> None:
        async for frame in self.transport.recv_frames():
            self.usage["audio_in_seconds"] += len(frame.data) / (
                self.config.input_sample_rate * 2
            )
            # Base64 JSON is the only audio path on this API (no raw binary).
            # The encode is the one unavoidable copy on this provider.
            await self._ws.send(json.dumps({
                "type": "input_audio_buffer.append",
                "audio": base64.b64encode(frame.data).decode("ascii"),
            }))
        await self.stop()

    async def _engine_loop(self) -> None:
        async for raw in self._ws:
            event = json.loads(raw)
            etype = event.get("type", "")

            # GA name first; legacy `response.audio.delta` still shows up on
            # some sessions (confirmed in the wild) — accept both forever.
            if etype in ("response.output_audio.delta", "response.audio.delta"):
                if self._response_started_at:
                    _record(self, "tts_first_byte",
                            (time.monotonic() - self._response_started_at) * 1000.0)
                    self._response_started_at = 0.0
                self.set_state(SessionState.SPEAKING)
                pcm = base64.b64decode(event["delta"])
                self.usage["audio_out_seconds"] += len(pcm) / (
                    self.config.output_sample_rate * 2
                )
                self.enqueue_audio(pcm)

            elif etype == "conversation.item.input_audio_transcription.completed":
                await self.on_user_transcript(event.get("transcript", ""))

            elif etype == "input_audio_buffer.speech_started":
                # Server auto-cancels the active response (interrupt_response);
                # we still must flush OUR queue — output_audio_buffer.clear is
                # WebRTC-only, playback buffering is our job on WebSocket.
                await self.on_user_speech_started()

            elif etype == "response.created":
                self._active_response_id = event.get("response", {}).get("id")
                self._response_started_at = time.monotonic()

            elif etype == "response.done":
                self._active_response_id = None
                self._accumulate_usage(
                    (event.get("response") or {}).get("usage") or {}
                )
                if self.state is SessionState.SPEAKING:
                    self.set_state(SessionState.LISTENING)

            elif etype == "error":
                logger.error("openai realtime error: %s", event.get("error"))

    def _accumulate_usage(self, usage: dict[str, Any]) -> None:
        """Sum a response.done usage block. Metering must never break the
        audio path — surprise shapes are logged and ignored."""
        try:
            self.usage["input_tokens"] += usage.get("input_tokens", 0) or 0
            self.usage["output_tokens"] += usage.get("output_tokens", 0) or 0
            for details_key, prefix in (
                ("input_token_details", "input_tokens"),
                ("output_token_details", "output_tokens"),
            ):
                details = usage.get(details_key) or {}
                self.usage[f"{prefix}_text"] += details.get("text_tokens", 0) or 0
                self.usage[f"{prefix}_audio"] += details.get("audio_tokens", 0) or 0
            self.usage["turns"] += 1
        except Exception as exc:
            logger.debug("realtime usage parse skipped: %r", exc)

    async def speak_text(self, text: str, *, interrupt: bool = False) -> None:
        if interrupt:
            if self._active_response_id is not None:
                await self._ws.send(json.dumps({"type": "response.cancel"}))
            await self.interrupt_playback()
        # Out-of-band response: deterministic delivery, does not pollute the
        # realtime conversation (dialog history lives in the bridge).
        await self._ws.send(json.dumps({
            "type": "response.create",
            "response": {
                "conversation": "none",
                "output_modalities": ["audio"],
                "instructions": (
                    f"Say exactly the following to the user in "
                    f"{self.config.language}, {self.config.tone} tone, and "
                    f"nothing else: {text}"
                ),
            },
        }))

    async def close(self) -> None:
        if self._ws is not None:
            with contextlib.suppress(Exception):
                await self._ws.close()
            self._ws = None
        await self.transport.close()

