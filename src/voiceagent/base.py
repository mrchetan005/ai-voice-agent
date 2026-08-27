"""Streaming pipeline contracts: transports, queues, phrase cache, and the
abstract voice proxy that all provider engines implement.

Latency design notes
--------------------
* Audio moves as the *same* ``bytes`` object end-to-end (socket -> queue ->
  socket).  No decode, no re-encode, no per-frame copies.
* Every queue is bounded.  The audio-out queue drops the OLDEST frame under
  pressure: in realtime voice, stale audio is worse than a dropped frame —
  playing 2-second-old speech reads as lag; a 20 ms gap is inaudible.
* All loops live in one ``asyncio.TaskGroup`` so a failure in any leg
  cancels the whole session deterministically instead of leaking tasks.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import threading
import urllib.parse
from abc import ABC, abstractmethod
from collections.abc import AsyncIterator, Awaitable, Callable, Iterable
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

from .models import AudioFrame, SessionConfig, SessionState

if TYPE_CHECKING:  # circular at runtime: bridge holds a proxy reference
    from .commentary_and_approval import OrchestratorBridge

logger = logging.getLogger("voiceagent")


# ---------------------------------------------------------------------------
# Queue plumbing
# ---------------------------------------------------------------------------


class EndOfStream:
    """Sentinel marking the end of a stream inside a queue."""

    __slots__ = ()

    def __repr__(self) -> str:  # pragma: no cover - debug aid
        return "<EndOfStream>"


END_OF_STREAM = EndOfStream()


def put_drop_oldest(queue: asyncio.Queue[Any], item: Any) -> None:
    """Non-blocking put that evicts the oldest entry when full.

    Used only for the audio-out path (see module docstring for why oldest,
    not newest).  Control/pulse queues must never drop — they use plain
    ``await queue.put`` with generous bounds instead.
    """
    while True:
        try:
            queue.put_nowait(item)
            return
        except asyncio.QueueFull:
            with contextlib.suppress(asyncio.QueueEmpty):
                queue.get_nowait()


def drain_queue(queue: asyncio.Queue[Any]) -> int:
    """Drop everything currently buffered; returns number of items dropped."""
    dropped = 0
    with contextlib.suppress(asyncio.QueueEmpty):
        while True:
            queue.get_nowait()
            dropped += 1
    return dropped


# ---------------------------------------------------------------------------
# Transports
# ---------------------------------------------------------------------------


@runtime_checkable
class AudioTransport(Protocol):
    """Bidirectional audio pipe between the human and this proxy.

    Implementations: :class:`WebSocketTransport` (browser/phone gateway),
    :class:`LocalAudioTransport` (mic/speakers), ``MockTransport`` (eval).
    A WebRTC transport slots in here later without touching any engine —
    it only needs to satisfy this protocol.
    """

    def recv_frames(self) -> AsyncIterator[AudioFrame]:
        """Yield inbound user audio frames until the peer disconnects."""
        ...

    async def send_frame(self, frame: AudioFrame) -> None: ...

    async def interrupt(self) -> None:
        """Tell the playback side to flush anything not yet played (barge-in)."""
        ...

    async def close(self) -> None: ...


class WebSocketTransport:
    """One connected WebSocket peer.

    Wire contract (documented for client authors):
      * client -> server BINARY frames: raw PCM16 mono at the session's
        ``input_sample_rate``.
      * server -> client BINARY frames: raw PCM16 mono at
        ``output_sample_rate``.
      * server -> client TEXT ``{"type": "clear"}``: flush your local
        playback buffer immediately (barge-in).
      * per-connection config comes from query params on the WS path
        (``?language=hi-IN&tone=warm``) — no in-band handshake message, so
        there is no first-message race.
    """

    def __init__(self, connection: Any, input_sample_rate: int) -> None:
        self._conn = connection
        self._input_rate = input_sample_rate

    @property
    def config_overrides(self) -> dict[str, str]:
        """Session overrides parsed from the connection's query string."""
        path = getattr(getattr(self._conn, "request", None), "path", "") or ""
        query = urllib.parse.urlsplit(path).query
        return {k: v[0] for k, v in urllib.parse.parse_qs(query).items()}

    async def recv_frames(self) -> AsyncIterator[AudioFrame]:
        try:
            async for message in self._conn:
                if isinstance(message, bytes):
                    # Zero-copy: the websockets library hands us the payload
                    # bytes; we wrap, never slice.
                    yield AudioFrame(data=message, sample_rate=self._input_rate)
                # Text frames are reserved for future client control messages;
                # ignoring unknown ones keeps old clients compatible.
        except Exception as exc:  # connection closed by peer
            logger.debug("ws transport recv ended: %r", exc)

    async def send_frame(self, frame: AudioFrame) -> None:
        await self._conn.send(frame.data)

    async def interrupt(self) -> None:
        with contextlib.suppress(Exception):
            await self._conn.send(json.dumps({"type": "clear"}))

    async def close(self) -> None:
        with contextlib.suppress(Exception):
            await self._conn.close()


class LocalAudioTransport:
    """Microphone/speaker transport via ``sounddevice`` (optional extra).

    Audio callbacks run on PortAudio's thread; frames cross into asyncio via
    ``loop.call_soon_threadsafe``.  Playback uses a lock-guarded bytearray
    the output callback consumes — clearing that buffer IS the barge-in
    flush, so interruption latency equals one hardware block (~10-20 ms).
    """

    def __init__(
        self,
        input_sample_rate: int = 16_000,
        output_sample_rate: int = 24_000,
        block_ms: int = 20,
    ) -> None:
        try:
            import sounddevice
        except ImportError as exc:  # pragma: no cover - env dependent
            raise RuntimeError(
                'Local audio needs the "local" extra: uv add "voiceagent[local]"'
            ) from exc
        self._sd = sounddevice
        self._input_rate = input_sample_rate
        self._output_rate = output_sample_rate
        self._block_in = int(input_sample_rate * block_ms / 1000)
        self._in_queue: asyncio.Queue[bytes | EndOfStream] = asyncio.Queue(maxsize=256)
        self._out_buffer = bytearray()
        self._out_lock = threading.Lock()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._streams: list[Any] = []
        self._closed = False

    def _start_streams(self) -> None:
        loop = asyncio.get_running_loop()
        self._loop = loop

        def on_input(indata: Any, frames: int, time_info: Any, status: Any) -> None:
            data = bytes(indata)  # one unavoidable copy out of the C buffer
            loop.call_soon_threadsafe(put_drop_oldest, self._in_queue, data)

        def on_output(outdata: Any, frames: int, time_info: Any, status: Any) -> None:
            need = len(outdata)
            with self._out_lock:
                chunk = bytes(self._out_buffer[:need])
                del self._out_buffer[:need]
            outdata[: len(chunk)] = chunk
            if len(chunk) < need:  # underrun -> silence, never block the callback
                outdata[len(chunk) :] = b"\x00" * (need - len(chunk))

        in_stream = self._sd.RawInputStream(
            samplerate=self._input_rate,
            blocksize=self._block_in,
            channels=1,
            dtype="int16",
            callback=on_input,
        )
        out_stream = self._sd.RawOutputStream(
            samplerate=self._output_rate,
            channels=1,
            dtype="int16",
            callback=on_output,
        )
        in_stream.start()
        out_stream.start()
        self._streams = [in_stream, out_stream]

    async def recv_frames(self) -> AsyncIterator[AudioFrame]:
        if not self._streams:
            self._start_streams()
        while True:
            item = await self._in_queue.get()
            if isinstance(item, EndOfStream):
                return
            yield AudioFrame(data=item, sample_rate=self._input_rate)

    async def send_frame(self, frame: AudioFrame) -> None:
        if not self._streams:
            self._start_streams()
        with self._out_lock:
            self._out_buffer.extend(frame.data)

    async def interrupt(self) -> None:
        with self._out_lock:
            self._out_buffer.clear()

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._loop is not None:
            self._loop.call_soon_threadsafe(
                put_drop_oldest, self._in_queue, END_OF_STREAM
            )
        for stream in self._streams:
            with contextlib.suppress(Exception):
                stream.stop()
                stream.close()


# ---------------------------------------------------------------------------
# Phrase cache (local audio edge buffer)
# ---------------------------------------------------------------------------

# Stock fillers spoken while the orchestrator works.  Cached as raw PCM at
# session start so their TTFB is ~0 instead of a full TTS round trip.
DEFAULT_FILLER_PHRASES: dict[str, list[str]] = {
    "en": ["Hold on.", "Checking that.", "One moment.", "Working on it."],
    "hi": ["एक मिनट रुकिए।", "देख रहा हूँ।", "बस एक पल।"],
    "es": ["Un momento.", "Déjame ver.", "Ya casi."],
}


class PhraseCache:
    """Pre-synthesized PCM keyed by (phrase, language, voice)."""

    def __init__(self) -> None:
        self._store: dict[tuple[str, str, str], bytes] = {}

    @staticmethod
    def _key(phrase: str, language: str, voice: str | None) -> tuple[str, str, str]:
        return (phrase.strip().lower(), language.split("-")[0].lower(), voice or "")

    def get(self, phrase: str, language: str, voice: str | None) -> bytes | None:
        return self._store.get(self._key(phrase, language, voice))

    def put(self, phrase: str, language: str, voice: str | None, pcm: bytes) -> None:
        self._store[self._key(phrase, language, voice)] = pcm

    async def warm(
        self,
        synthesize: Callable[[str], Awaitable[bytes]],
        language: str,
        voice: str | None,
        phrases: Iterable[str] | None = None,
    ) -> int:
        """Synthesize and cache fillers; returns how many were cached.

        Runs sequentially on purpose: warming races the first real utterance
        for TTS capacity, so we keep it low-priority rather than parallel.
        """
        lang = language.split("-")[0].lower()
        todo = list(phrases or DEFAULT_FILLER_PHRASES.get(lang, DEFAULT_FILLER_PHRASES["en"]))
        cached = 0
        for phrase in todo:
            if self.get(phrase, language, voice) is not None:
                continue
            try:
                pcm = await synthesize(phrase)
            except Exception as exc:
                logger.debug("phrase warm failed for %r: %r", phrase, exc)
                continue
            if pcm:
                self.put(phrase, language, voice, pcm)
                cached += 1
        return cached


# ---------------------------------------------------------------------------
# Abstract proxy
# ---------------------------------------------------------------------------


class BaseVoiceAgentProxy(ABC):
    """Contract every provider engine implements.

    Concrete engines own the provider connection and three loops that run
    concurrently in one TaskGroup:

    * ``_uplink_loop``   — user audio frames -> provider
    * ``_engine_loop``   — provider events   -> audio out / transcripts / tools
    * ``_downlink_loop`` — audio-out queue   -> user transport (shared impl)

    The bridge (commentary + approvals + agent handler) is bound after
    construction via :meth:`bind` because bridge and proxy reference each
    other.
    """

    # ~64 frames of 20 ms audio = ~1.3 s of buffered speech. Bigger buffers
    # only add barge-in flush latency; smaller ones underrun on jittery nets.
    AUDIO_OUT_QUEUE_SIZE = 64

    def __init__(self, config: SessionConfig, transport: AudioTransport) -> None:
        self.config = config
        self.transport = transport
        self.state: SessionState = SessionState.IDLE
        self.phrase_cache = PhraseCache()
        self._audio_out: asyncio.Queue[bytes | EndOfStream] = asyncio.Queue(
            maxsize=self.AUDIO_OUT_QUEUE_SIZE
        )
        self._bridge: OrchestratorBridge | None = None
        # Optional TelemetryRecorder, attached by the facade. Kept as a loose
        # attribute so providers don't import the telemetry module.
        self.telemetry: Any = None
        # When an approval window is open, transcripts resolve this future
        # instead of triggering a normal agent turn.
        self._listen_waiter: asyncio.Future[str] | None = None
        self._stopping = asyncio.Event()

    # -- wiring ------------------------------------------------------------

    def bind(self, bridge: OrchestratorBridge) -> None:
        self._bridge = bridge

    def set_state(self, state: SessionState) -> None:
        if state is not self.state:
            logger.debug("session %s: %s -> %s", self.config.session_id, self.state, state)
            self.state = state

    # -- lifecycle ----------------------------------------------------------

    async def run(self) -> None:
        """Connect and pump all loops until the session ends.

        Lifecycle rule: the FIRST loop to finish ends the session — uplink
        ending means the user hung up, engine ending means the provider
        closed, and ``stop()`` is an explicit request.  Waiting for ALL
        loops would leak sessions (e.g. an engine still blocked on its
        provider socket after the client disconnected).
        """
        await self._connect()
        self.set_state(SessionState.LISTENING)
        loops = [
            asyncio.create_task(self._uplink_loop(), name="uplink"),
            asyncio.create_task(self._engine_loop(), name="engine"),
            asyncio.create_task(self._downlink_loop(), name="downlink"),
        ]
        stopper = asyncio.create_task(self._stopping.wait(), name="stopper")
        try:
            done, _pending = await asyncio.wait(
                [*loops, stopper], return_when=asyncio.FIRST_COMPLETED
            )
            for task in done:  # surface a crash from whichever loop died first
                if task is not stopper and not task.cancelled() and task.exception():
                    raise task.exception()  # type: ignore[misc]
        finally:
            for task in (*loops, stopper):
                task.cancel()
            await asyncio.gather(*loops, stopper, return_exceptions=True)
            self.set_state(SessionState.CLOSED)
            await self.close()

    async def stop(self) -> None:
        self._stopping.set()
        put_drop_oldest(self._audio_out, END_OF_STREAM)

    # -- shared downlink -----------------------------------------------------

    async def _downlink_loop(self) -> None:
        """Drain synthesized audio to the user transport.

        This is the only writer to the transport's audio path, so barge-in
        (which drains the queue) cannot interleave with half-sent frames.
        """
        while True:
            item = await self._audio_out.get()
            if isinstance(item, EndOfStream):
                return
            await self.transport.send_frame(
                AudioFrame(data=item, sample_rate=self.config.output_sample_rate)
            )

    def enqueue_audio(self, pcm: bytes) -> None:
        put_drop_oldest(self._audio_out, pcm)

    async def interrupt_playback(self) -> None:
        """Barge-in: flush everything queued locally and at the client."""
        dropped = drain_queue(self._audio_out)
        await self.transport.interrupt()
        if dropped:
            logger.debug("barge-in flushed %d queued frames", dropped)

    # -- transcript / speech routing ------------------------------------------

    async def on_user_transcript(self, text: str) -> None:
        """Called by engines whenever a FINAL user transcript is ready."""
        text = text.strip()
        if not text:
            return
        waiter = self._listen_waiter
        if waiter is not None and not waiter.done():
            # An approval window is open: the utterance is the verdict, not
            # a new task for the agent.
            waiter.set_result(text)
            return
        if self._bridge is not None:
            await self._bridge.handle_user_utterance(text)

    async def on_user_speech_started(self) -> None:
        """Called by engines on VAD speech-start; implements barge-in."""
        if not self.config.allow_barge_in:
            return
        if self.state is SessionState.SPEAKING or self._audio_out.qsize() > 0:
            await self.interrupt_playback()
            if self._bridge is not None:
                await self._bridge.on_barge_in()

    async def open_listen_window(self, timeout_s: float) -> str | None:
        """Block until the next final user transcript (HITL approvals).

        Returns None on timeout.  Engines keep streaming mic audio the whole
        time — "opening the mic" is a routing change, not a device change.
        """
        loop = asyncio.get_running_loop()
        self._listen_waiter = loop.create_future()
        self.set_state(SessionState.AWAITING_APPROVAL)
        try:
            return await asyncio.wait_for(self._listen_waiter, timeout=timeout_s)
        except TimeoutError:
            return None
        finally:
            self._listen_waiter = None
            self.set_state(SessionState.LISTENING)

    async def speak_token_stream(self, chunks: AsyncIterator[str]) -> bool:
        """Optional fast path: pipe an LLM token stream straight into TTS.

        Split-stack engines override this to feed tokens into a TTS
        continuation context (first 3-5 words flushed immediately, minimal
        TTFB).  The default returns False WITHOUT consuming the iterator so
        the bridge can fall back to clause-by-clause ``speak_text``.
        """
        return False

    # -- provider-specific ------------------------------------------------------

    @abstractmethod
    async def _connect(self) -> None:
        """Open provider connection(s) and finish the protocol handshake."""

    @abstractmethod
    async def _uplink_loop(self) -> None:
        """Pump ``transport.recv_frames()`` into the provider."""

    @abstractmethod
    async def _engine_loop(self) -> None:
        """Consume provider events: audio deltas, transcripts, tool calls."""

    @abstractmethod
    async def speak_text(self, text: str, *, interrupt: bool = False) -> None:
        """Say ``text`` to the user in the session language/tone.

        ``interrupt=True`` flushes current playback first (urgent messages
        like approval prompts).  Implementations must return once synthesis
        is enqueued — never block until playback finishes.
        """

    @abstractmethod
    async def close(self) -> None:
        """Release provider connections. Must be idempotent."""
