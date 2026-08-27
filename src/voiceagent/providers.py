"""Concrete provider engines.

Three architectures behind one contract (:class:`BaseVoiceAgentProxy`):

* ``OpenAIRealtimeProxy`` — native omni engine in STRICT PROXY mode: the
  realtime model does ASR + TTS, but ``create_response`` is disabled so the
  model never answers on its own.  Final user transcripts route to the
  backend agent; every spoken line is an out-of-band "say exactly" response.
* ``GeminiLiveProxy`` — native omni engine in MODEL-FRONTED mode: Gemini
  Live cannot suppress its own answers, so the backend agent is mounted as
  the mandatory ``send_to_agent`` tool and the model voices its results.
* ``SplitStackProxy`` — Deepgram ASR + any OpenAI-compatible LLM + Cartesia
  or ElevenLabs TTS, with token-level chunking into TTS continuations.

All wire messages match provider docs as verified Aug 2026 (see plan).
No vendor SDKs: raw ``websockets`` + ``httpx`` keeps the protocol visible
and the dependency surface tiny; swapping to an SDK later touches one class.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import json
import logging
import math
import os
import re
import struct
import time
from collections.abc import AsyncIterator, Callable
from typing import Any

import httpx
import websockets

from .base import BaseVoiceAgentProxy, EndOfStream
from .models import SessionState

logger = logging.getLogger("voiceagent")


def _require_env(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise RuntimeError(f"Missing required environment variable {name}")
    return value


def _record(proxy: BaseVoiceAgentProxy, metric: str, ms: float) -> None:
    if proxy.telemetry is not None:
        proxy.telemetry.record(metric, ms)


# ---------------------------------------------------------------------------
# Token chunking (split-stack TTFB optimization)
# ---------------------------------------------------------------------------

_CLAUSE_PUNCT = re.compile(r"[.,;:!?।]")


async def chunk_tokens(
    tokens: AsyncIterator[str],
    first_words: int = 4,
    max_words: int = 12,
) -> AsyncIterator[str]:
    """Regroup an LLM token stream into TTS-friendly text pieces.

    The FIRST piece flushes as soon as ~``first_words`` words (or any
    punctuation) arrive — that is the single biggest TTFB lever in a split
    stack: TTS starts synthesizing while the LLM is still on token 6.
    Subsequent pieces flush on clause punctuation or ``max_words``, which
    keeps prosody natural (TTS engines phrase better on clause boundaries).
    """
    buffer: list[str] = []
    first_flushed = False
    async for token in tokens:
        if not token:
            continue
        buffer.append(token)
        joined = "".join(buffer)
        words = len(joined.split())
        threshold = first_words if not first_flushed else max_words
        if words >= threshold or (_CLAUSE_PUNCT.search(joined) and words >= 2):
            yield joined
            buffer.clear()
            first_flushed = True
    if buffer:
        yield "".join(buffer)


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
                self.enqueue_audio(base64.b64decode(event["delta"]))

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
                if self.state is SessionState.SPEAKING:
                    self.set_state(SessionState.LISTENING)

            elif etype == "error":
                logger.error("openai realtime error: %s", event.get("error"))

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
                    self.enqueue_audio(base64.b64decode(inline["data"]))
            if content.get("turnComplete"):
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
            "vad_events": "true",
            "smart_format": "true",
        }
        query = "&".join(f"{k}={v}" for k, v in params.items())
        self._url = f"wss://api.deepgram.com/v1/listen?{query}"
        self._ws: Any = None
        self._keepalive: asyncio.Task[None] | None = None

    async def connect(self) -> None:
        self._ws = await websockets.connect(
            self._url,
            additional_headers={"Authorization": f"Token {_require_env('DEEPGRAM_API_KEY')}"},
        )
        # Deepgram closes idle sockets (~10 s, NET-0001); KeepAlive every 5 s
        # is harmless during active audio and mandatory during user silence.
        self._keepalive = asyncio.create_task(self._keepalive_loop())

    async def _keepalive_loop(self) -> None:
        while True:
            await asyncio.sleep(5.0)
            with contextlib.suppress(Exception):
                await self._ws.send(json.dumps({"type": "KeepAlive"}))

    async def send_audio(self, pcm: bytes) -> None:
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

    async def connect(self) -> None:
        self._ws = await websockets.connect(
            self._url,
            additional_headers={"Authorization": f"Token {_require_env('DEEPGRAM_API_KEY')}"},
        )

    async def send_audio(self, pcm: bytes) -> None:
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


# ---------------------------------------------------------------------------
# Split stack: OpenAI-compatible LLM (Groq default)
# ---------------------------------------------------------------------------


class OpenAICompatLLM:
    """Streaming chat-completions client for any OpenAI-compatible endpoint."""

    def __init__(self, options: dict[str, Any]) -> None:
        self.base_url: str = options.get("llm_base_url", "https://api.groq.com/openai/v1")
        self.model: str = options.get("llm_model", "llama-3.3-70b-versatile")
        self._key_env: str = options.get("llm_api_key_env", "GROQ_API_KEY")
        self._extra: dict[str, Any] = dict(options.get("llm_extra", {}))
        # gpt-oss reasoning models: force low effort for voice TTFB.
        if "gpt-oss" in self.model and "reasoning_effort" not in self._extra:
            self._extra["reasoning_effort"] = "low"
        self._client = httpx.AsyncClient(timeout=httpx.Timeout(60.0, connect=10.0))

    async def stream(
        self,
        messages: list[dict[str, str]],
        on_first_token: Callable[[float], None] | None = None,
    ) -> AsyncIterator[str]:
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "stream": True,
            **self._extra,
        }
        started = time.monotonic()
        first = True
        async with self._client.stream(
            "POST",
            f"{self.base_url}/chat/completions",
            json=payload,
            headers={"Authorization": f"Bearer {_require_env(self._key_env)}"},
        ) as response:
            response.raise_for_status()
            async for line in response.aiter_lines():
                if not line.startswith("data: "):
                    continue
                data = line[6:]
                if data == "[DONE]":
                    return
                delta = (
                    json.loads(data).get("choices", [{}])[0]
                    .get("delta", {})
                    .get("content")
                )
                if delta:
                    if first and on_first_token is not None:
                        on_first_token((time.monotonic() - started) * 1000.0)
                        first = False
                    yield delta

    async def aclose(self) -> None:
        await self._client.aclose()


def make_llm_agent(llm: OpenAICompatLLM) -> Callable[[Any], AsyncIterator[str]]:
    """Default agent when the user plugs none: a plain streaming LLM chat.

    The returned handler follows the normal AgentContext contract, so
    swapping it for a real orchestrator later is one constructor argument.
    """

    def handler(ctx: Any) -> AsyncIterator[str]:
        messages = [{"role": "system", "content": (
            f"{ctx.config.system_prompt} Respond in {ctx.config.language} "
            f"with a {ctx.config.tone} tone. Keep answers short: this is a "
            f"voice conversation."
        )}]
        messages.extend(ctx.history[-12:])  # trailing window; voice = short turns
        return llm.stream(messages)

    return handler


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

    @staticmethod
    async def _send_chunks(ws: Any, chunks: AsyncIterator[str]) -> None:
        async for piece in chunks:
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
        self._warm_task: asyncio.Task[int] | None = None

    async def _connect(self) -> None:
        started = time.monotonic()
        await self.asr.connect()
        await self.tts.open()
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
                _record(self, "tts_first_byte", (time.monotonic() - first_byte) * 1000.0)
            self.enqueue_audio(pcm)

        await self.tts.speak(chunk_tokens(chunks), on_pcm)
        self.set_state(SessionState.LISTENING)
        return True

    async def close(self) -> None:
        if self._warm_task is not None:
            self._warm_task.cancel()
        await self.asr.close()
        await self.tts.close()
        await self.llm.aclose()
        await self.transport.close()


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
