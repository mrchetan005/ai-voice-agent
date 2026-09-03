"""Gemini Live engine (model-fronted: native audio + native tools or send_to_agent)."""

from __future__ import annotations

import asyncio
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
# Gemini Live (model-fronted tool-bridge mode)
# ---------------------------------------------------------------------------


class GeminiLiveProxy(BaseVoiceAgentProxy):
    """Gemini Live over the raw BidiGenerateContent WebSocket.

    Gemini native-audio models always voice their own replies, so the strict
    proxy inverts: the model is REQUIRED (via system instruction + tool
    declaration) to route every user request through ``send_to_agent`` and
    to relay the returned ``speech`` verbatim.  Input is 16 kHz PCM16,
    output 24 kHz.  Connections live ~10 minutes: ``goAway`` triggers a
    reconnect that resumes via the last ``sessionResumptionUpdate`` handle.
    """

    URL = (
        "wss://generativelanguage.googleapis.com/ws/"
        "google.ai.generativelanguage.v1beta.GenerativeService.BidiGenerateContent"
    )

    def __init__(self, config: Any, transport: Any) -> None:
        super().__init__(config, transport)
        opts = config.provider_options
        self._model: str = opts.get("model", "models/gemini-3.1-flash-live-preview")
        self._voice: str = config.voice_id or opts.get("voice", "Despina")
        # SINGLE-BRAIN mode: provider_options["native_tools"] maps tool name
        # -> {"declaration": <functionDeclaration dict>, "handler": async fn}.
        # Gemini then calls YOUR tools directly (one LLM, lowest latency).
        # Without it, DUAL-BRAIN mode applies: the mandatory send_to_agent
        # tool proxies every request to the bridge's agent handler.
        self._native_tools: dict[str, Any] = opts.get("native_tools") or {}
        # Optional async callback(role, text) receiving aggregated turn
        # transcripts ("user" / "assistant") — persistence, analytics, QA.
        self._on_transcription: Any = opts.get("on_transcription")
        self._ws: Any = None
        self._resume_handle: str | None = None
        self._reconnect_requested = asyncio.Event()
        self._input_transcript: list[str] = []
        self._output_transcript: list[str] = []
        self._tool_tasks: set[asyncio.Task[None]] = set()
        # Neutral per-session usage counters (survive goAway reconnects).
        # Token counts come from the API's usageMetadata; audio seconds are
        # counted from PCM bytes as an independent billing cross-check.
        self.usage: dict[str, float] = {
            "prompt_tokens": 0.0, "prompt_tokens_text": 0.0,
            "prompt_tokens_audio": 0.0, "response_tokens": 0.0,
            "response_tokens_text": 0.0, "response_tokens_audio": 0.0,
            "total_tokens": 0.0, "audio_in_seconds": 0.0,
            "audio_out_seconds": 0.0, "turns": 0.0,
        }

    def _setup_message(self) -> dict[str, Any]:
        if self._native_tools:
            # Single-brain: the live model IS the agent; its tools are ours.
            instruction = self.config.system_prompt
            declarations = [t["declaration"] for t in self._native_tools.values()]
        else:
            instruction = (
                f"{self.config.system_prompt}\n"
                f"You are the VOICE INTERFACE for a backend agent. For EVERY "
                f"user request, you MUST call the send_to_agent tool with the "
                f"user's request as `query`. Never answer from your own "
                f"knowledge. When the tool returns, say its `speech` field "
                f"verbatim. Always speak {self.config.language} with a "
                f"{self.config.tone} tone."
            )
            declarations = [{
                "name": "send_to_agent",
                "description": "Forward the user's request to the backend "
                               "agent and get the text to speak back.",
                "parameters": {
                    "type": "OBJECT",
                    "properties": {"query": {"type": "STRING"}},
                    "required": ["query"],
                },
            }]
        return {
            "setup": {
                "model": self._model,
                "generationConfig": {
                    "responseModalities": ["AUDIO"],
                    "speechConfig": {
                        "voiceConfig": {"prebuiltVoiceConfig": {"voiceName": self._voice}}
                    },
                },
                "systemInstruction": {"parts": [{"text": instruction}]},
                "tools": [{"functionDeclarations": declarations}],
                "realtimeInputConfig": {
                    "automaticActivityDetection": {"disabled": False},
                    "activityHandling": "START_OF_ACTIVITY_INTERRUPTS",
                },
                "inputAudioTranscription": {},
                "outputAudioTranscription": {},
                # Unlimited session length via sliding-window compression.
                "contextWindowCompression": {"slidingWindow": {}},
                "sessionResumption": {"handle": self._resume_handle},
            }
        }

    async def _connect(self) -> None:
        started = time.monotonic()
        key = _require_env("GOOGLE_API_KEY")
        self._ws = await websockets.connect(
            f"{self.URL}?key={key}", max_size=16 * 1024 * 1024
        )
        await self._ws.send(json.dumps(self._setup_message()))
        # Nothing may be sent until setupComplete arrives.
        while True:
            raw = await self._ws.recv()
            if isinstance(raw, bytes):
                raw = raw.decode("utf-8")
            if "setupComplete" in json.loads(raw):
                break
        _record(self, "handshake", (time.monotonic() - started) * 1000.0)

    async def _uplink_loop(self) -> None:
        async for frame in self.transport.recv_frames():
            self.usage["audio_in_seconds"] += len(frame.data) / (
                self.config.input_sample_rate * 2
            )
            with contextlib.suppress(websockets.ConnectionClosed):
                await self._ws.send(json.dumps({
                    "realtimeInput": {
                        "audio": {
                            "mimeType": f"audio/pcm;rate={self.config.input_sample_rate}",
                            "data": base64.b64encode(frame.data).decode("ascii"),
                        }
                    }
                }))
        await self.stop()

    async def _engine_loop(self) -> None:
        while True:
            try:
                async for raw in self._ws:
                    if isinstance(raw, bytes):
                        raw = raw.decode("utf-8")
                    await self._handle_message(json.loads(raw))
                    if self._reconnect_requested.is_set():
                        break
            except websockets.ConnectionClosed:
                pass
            if not self._reconnect_requested.is_set():
                return  # session genuinely over
            # goAway path: reopen with the resumption handle; uplink writes
            # during the gap are suppressed and resume on the new socket.
            self._reconnect_requested.clear()
            with contextlib.suppress(Exception):
                await self._ws.close()
            await self._connect()

    async def _handle_message(self, msg: dict[str, Any]) -> None:
        # usageMetadata rides as a top-level sibling of serverContent, and
        # the interrupted branch below returns early — so this check must
        # be non-exclusive and come first.
        if (usage_meta := msg.get("usageMetadata")) is not None:
            self._accumulate_usage(usage_meta)

        if (content := msg.get("serverContent")) is not None:
            if content.get("interrupted"):
                # Server-driven barge-in: generationComplete will NOT arrive
                # for this turn. Flush everything not yet played.
                await self.interrupt_playback()
                if self._bridge is not None:
                    await self._bridge.on_barge_in()
                await self._flush_transcripts()  # keep the partial turn
                self.set_state(SessionState.LISTENING)
                return
            if (transcription := content.get("inputTranscription")) and (
                text := transcription.get("text")
            ):
                self._input_transcript.append(text)
            if (transcription := content.get("outputTranscription")) and (
                text := transcription.get("text")
            ):
                self._output_transcript.append(text)
            for part in content.get("modelTurn", {}).get("parts", []):
                inline = part.get("inlineData")
                if inline and inline.get("data"):
                    self.set_state(SessionState.SPEAKING)
                    pcm = base64.b64decode(inline["data"])
                    self.usage["audio_out_seconds"] += len(pcm) / (
                        self.config.output_sample_rate * 2
                    )
                    self.enqueue_audio(pcm)
            if content.get("turnComplete"):
                self.usage["turns"] += 1
                self.set_state(SessionState.LISTENING)
                # The model replying marks the end of the user utterance.
                # Normal turns reach the agent via send_to_agent; the flush
                # below only feeds an OPEN APPROVAL WINDOW (the waiter).
                if self._input_transcript and self._listen_waiter is not None:
                    await self.on_user_transcript("".join(self._input_transcript))
                await self._flush_transcripts()

        elif (tool_call := msg.get("toolCall")) is not None:
            for call in tool_call.get("functionCalls", []):
                task = asyncio.create_task(self._run_tool(call))
                self._tool_tasks.add(task)
                task.add_done_callback(self._tool_tasks.discard)

        elif (cancellation := msg.get("toolCallCancellation")) is not None:
            # User barged in while the agent was working; drop those runs.
            logger.debug("gemini cancelled tool calls: %s", cancellation.get("ids"))
            for task in list(self._tool_tasks):
                task.cancel()

        elif (go_away := msg.get("goAway")) is not None:
            logger.info(
                "gemini goAway (timeLeft=%s); scheduling resume", go_away.get("timeLeft")
            )
            self._reconnect_requested.set()

        elif (update := msg.get("sessionResumptionUpdate")) is not None:
            if update.get("resumable") and update.get("newHandle"):
                self._resume_handle = update["newHandle"]

    def _accumulate_usage(self, meta: dict[str, Any]) -> None:
        """Sum a usageMetadata message into self.usage. Metering must never
        break the audio path, so any surprise shape is logged and ignored."""
        try:
            self.usage["prompt_tokens"] += meta.get("promptTokenCount", 0) or 0
            self.usage["response_tokens"] += meta.get("responseTokenCount", 0) or 0
            self.usage["total_tokens"] += meta.get("totalTokenCount", 0) or 0
            for details_key, prefix in (
                ("promptTokensDetails", "prompt_tokens"),
                ("responseTokensDetails", "response_tokens"),
            ):
                for detail in meta.get(details_key) or []:
                    modality = str(detail.get("modality", "")).lower()
                    if modality in ("text", "audio"):
                        self.usage[f"{prefix}_{modality}"] += detail.get("tokenCount", 0) or 0
        except Exception as exc:
            logger.debug("usageMetadata parse skipped: %r", exc)

    async def inject_text(self, text: str) -> None:
        """Public API: inject a user-role text turn into the live session
        (mid-call chat notes, connection nudges). Callers must NOT reach
        into the private WebSocket."""
        await self._ws.send(json.dumps({
            "clientContent": {
                "turns": [{"role": "user", "parts": [{"text": text}]}],
                "turnComplete": True,
            }
        }))

    async def _flush_transcripts(self) -> None:
        """Emit aggregated turn transcripts to log + optional callback."""
        for role, fragments in (
            ("user", self._input_transcript),
            ("assistant", self._output_transcript),
        ):
            text = "".join(fragments).strip()
            fragments.clear()
            if not text:
                continue
            logger.info("transcript[%s]: %s", role, text)
            if self._on_transcription is not None:
                try:
                    await self._on_transcription(role, text)
                except Exception:
                    logger.exception("on_transcription callback failed")

    async def _run_tool(self, call: dict[str, Any]) -> None:
        name = call.get("name", "send_to_agent")
        args = call.get("args") or {}
        started = time.monotonic()
        response: dict[str, Any]
        if name in self._native_tools:
            try:
                result = await self._native_tools[name]["handler"](args)
                response = result if isinstance(result, dict) else {"result": str(result)}
            except Exception as exc:
                logger.exception("native tool %s failed", name)
                response = {"error": str(exc)[:200]}
        elif self._bridge is not None:
            speech = await self._bridge.run_agent_collect(args.get("query", ""))
            response = {"speech": speech or "…"}
        else:
            response = {"speech": "…"}
        _record(self, "llm_ttfb", (time.monotonic() - started) * 1000.0)
        await self._ws.send(json.dumps({
            "toolResponse": {
                "functionResponses": [{
                    "id": call.get("id"),
                    "name": name,
                    "response": response,
                }]
            }
        }))

    async def speak_text(self, text: str, *, interrupt: bool = False) -> None:
        if interrupt:
            # No client-side cancel exists on this API; local flush only.
            await self.interrupt_playback()
        # Inject a directed turn; the model verbalizes it in-voice. Used for
        # commentary pulses and approval prompts while a tool call is open.
        await self._ws.send(json.dumps({
            "clientContent": {
                "turns": [{
                    "role": "user",
                    "parts": [{"text": (
                        "[SYSTEM TO ASSISTANT] Say exactly this to the user, "
                        f"nothing else: {text}"
                    )}],
                }],
                "turnComplete": True,
            }
        }))

    async def close(self) -> None:
        for task in list(self._tool_tasks):
            task.cancel()
        if self._ws is not None:
            with contextlib.suppress(Exception):
                await self._ws.close()
            self._ws = None
        await self.transport.close()

