"""WhatsApp call leg as a voiceagent AudioTransport, via aiortc WebRTC.

Meta's Calling API only exchanges SDP over the Graph API — media is a real
WebRTC peer connection we terminate ourselves. This class is the whole
bridge; the voice engines never know they're on a WhatsApp call.

Audio geometry:
  inbound  : Opus 48 kHz from Meta -> decoded by aiortc -> resampled to
             PCM16 16 kHz mono (Gemini Live input rate)
  outbound : PCM16 24 kHz mono from the engine -> resampled to 48 kHz ->
             Opus-encoded by aiortc, paced at 20 ms frames
Barge-in = clearing the outbound buffer: worst-case flush is one 20 ms frame.
"""

from __future__ import annotations

import asyncio
import contextlib
import fractions
import logging
import threading
import time
from collections.abc import AsyncIterator
from typing import Any

import av
from aiortc import RTCPeerConnection, RTCSessionDescription
from aiortc.mediastreams import MediaStreamTrack

from appointment_booker.webhooks import WebhookHub
from appointment_booker.whatsapp_api import WhatsAppClient
from voiceagent.base import END_OF_STREAM, EndOfStream, put_drop_oldest
from voiceagent.models import AudioFrame

logger = logging.getLogger("appointment_booker")

_OUT_RATE_WIRE = 48_000  # Opus native
_FRAME_MS = 20


def munge_sdp_for_meta(sdp: str) -> str:
    """Make an aiortc offer pass Meta's strict RFC 8866 SDP validator.

    Load-bearing fix (error 138008): aiortc emits THREE a=fingerprint lines
    (sha-256/384/512); Meta rejects any SDP with more than one — keep only
    sha-256 (same single munge pipecat's WhatsApp transport ships).
    Insurance: drop a=extmap (rejected from Chrome offers per field reports)
    and pin a=ptime:20 (Meta's documented Opus framing).
    """
    lines: list[str] = []
    for line in sdp.replace("\r\n", "\n").split("\n"):
        if not line:
            continue
        if line.startswith("a=fingerprint:") and not line.startswith("a=fingerprint:sha-256"):
            continue
        if line.startswith("a=extmap:"):
            continue
        lines.append(line)
        if line.startswith("a=rtpmap:") and "opus" in line and "a=ptime:20" not in sdp:
            lines.append("a=ptime:20")
    return "\r\n".join(lines) + "\r\n"


class OutboundAudioTrack(MediaStreamTrack):
    """Pulls engine PCM (24 kHz) from a buffer, emits paced 48 kHz frames.

    The buffer is lock-guarded because ``send_frame`` (event loop) and
    ``recv`` (aiortc's sender task) race on it; clearing it is barge-in.
    """

    kind = "audio"

    def __init__(self, engine_rate: int = 24_000) -> None:
        super().__init__()
        self._engine_rate = engine_rate
        self._samples_per_frame = int(engine_rate * _FRAME_MS / 1000)
        self._buffer = bytearray()
        self._lock = threading.Lock()
        self._resampler = av.AudioResampler(format="s16", layout="mono", rate=_OUT_RATE_WIRE)
        self._pts = 0  # in wire-rate samples
        self._start: float | None = None

    def write(self, pcm: bytes) -> None:
        with self._lock:
            self._buffer.extend(pcm)

    def clear(self) -> None:
        with self._lock:
            self._buffer.clear()

    async def recv(self) -> av.AudioFrame:
        # Wall-clock pacing: one frame per 20 ms, silence on underrun. Never
        # block on the engine — a stalled TTS must not stall RTP timing.
        if self._start is None:
            self._start = time.monotonic()
        target = self._start + self._pts / _OUT_RATE_WIRE
        delay = target - time.monotonic()
        if delay > 0:
            await asyncio.sleep(delay)

        need = self._samples_per_frame * 2  # s16 mono
        with self._lock:
            chunk = bytes(self._buffer[:need])
            del self._buffer[:need]
        if len(chunk) < need:
            chunk += b"\x00" * (need - len(chunk))

        frame = av.AudioFrame(format="s16", layout="mono", samples=self._samples_per_frame)
        frame.planes[0].update(chunk)
        frame.sample_rate = self._engine_rate
        frame.pts = None  # let the resampler assign
        resampled = self._resampler.resample(frame)
        out = resampled[0] if isinstance(resampled, list) else resampled
        out.pts = self._pts
        out.sample_rate = _OUT_RATE_WIRE
        out.time_base = fractions.Fraction(1, _OUT_RATE_WIRE)
        self._pts += int(_OUT_RATE_WIRE * _FRAME_MS / 1000)
        return out


class WhatsAppCallTransport:
    """voiceagent.base.AudioTransport over a Meta WhatsApp WebRTC call."""

    def __init__(
        self,
        wa: WhatsAppClient,
        hub: WebhookHub,
        to: str,
        input_sample_rate: int = 16_000,
        output_sample_rate: int = 24_000,
    ) -> None:
        self._wa = wa
        self._hub = hub
        self._to = to
        self._input_rate = input_sample_rate
        self._pc = RTCPeerConnection()
        self._out_track = OutboundAudioTrack(engine_rate=output_sample_rate)
        self._in_queue: asyncio.Queue[bytes | EndOfStream] = asyncio.Queue(maxsize=256)
        self._in_resampler = av.AudioResampler(format="s16", layout="mono", rate=input_sample_rate)
        self.call_id: str | None = None
        self._tasks: list[asyncio.Task[None]] = []
        self._closed = False

    # -- call setup -------------------------------------------------------------

    async def place_call(self, answer_timeout_s: float = 30.0) -> None:
        self._pc.addTrack(self._out_track)

        @self._pc.on("track")
        def on_track(track: MediaStreamTrack) -> None:
            if track.kind == "audio":
                self._tasks.append(asyncio.create_task(self._pump_inbound(track)))

        offer = await self._pc.createOffer()
        # aiortc completes ICE gathering inside setLocalDescription, so the
        # SDP below already carries the candidates Meta needs.
        await self._pc.setLocalDescription(offer)

        response = await self._wa.initiate_call(
            self._to, munge_sdp_for_meta(self._pc.localDescription.sdp)
        )
        logger.info("initiate_call response: %s", response)
        self.call_id = (
            (response.get("calls") or [{}])[0].get("id")
            or response.get("call_id")
        )

        answer = await self._hub.wait_call_answer(answer_timeout_s)
        self.call_id = answer.call_id or self.call_id
        await self._pc.setRemoteDescription(
            RTCSessionDescription(sdp=answer.sdp, type="answer")
        )
        # End the session when Meta reports the user hung up.
        self._tasks.append(asyncio.create_task(self._watch_hangup()))
        logger.info("call connected (call_id=%s)", self.call_id)

    async def answer_call(self, call_id: str, offer_sdp: str) -> None:
        """Inbound leg: the caller's webhook `connect` carried an SDP OFFER;
        we answer via pre_accept + accept (same munged SDP both times)."""
        self.call_id = call_id
        self._pc.addTrack(self._out_track)

        @self._pc.on("track")
        def on_track(track: MediaStreamTrack) -> None:
            if track.kind == "audio":
                self._tasks.append(asyncio.create_task(self._pump_inbound(track)))

        await self._pc.setRemoteDescription(
            RTCSessionDescription(sdp=offer_sdp, type="offer")
        )
        answer = await self._pc.createAnswer()
        await self._pc.setLocalDescription(answer)  # waits for ICE gathering
        munged = munge_sdp_for_meta(self._pc.localDescription.sdp)
        await self._wa.pre_accept_call(call_id, munged)
        await self._wa.accept_call(call_id, munged)
        self._tasks.append(asyncio.create_task(self._watch_hangup()))
        logger.info("inbound call answered (call_id=%s)", call_id)

    async def _pump_inbound(self, track: MediaStreamTrack) -> None:
        try:
            while True:
                frame = await track.recv()
                for out in self._as_list(self._in_resampler.resample(frame)):
                    # s16 mono plane; slice off allocator padding.
                    pcm = bytes(out.planes[0])[: out.samples * 2]
                    put_drop_oldest(self._in_queue, pcm)
        except Exception as exc:
            logger.debug("inbound pump ended: %r", exc)
            put_drop_oldest(self._in_queue, END_OF_STREAM)

    @staticmethod
    def _as_list(resampled: Any) -> list[av.AudioFrame]:
        return resampled if isinstance(resampled, list) else [resampled]

    async def _watch_hangup(self) -> None:
        await self._hub.call_ended.wait()
        logger.info("remote hangup signalled")
        put_drop_oldest(self._in_queue, END_OF_STREAM)

    # -- AudioTransport protocol ---------------------------------------------------

    async def recv_frames(self) -> AsyncIterator[AudioFrame]:
        while True:
            item = await self._in_queue.get()
            if isinstance(item, EndOfStream):
                return
            yield AudioFrame(data=item, sample_rate=self._input_rate)

    async def send_frame(self, frame: AudioFrame) -> None:
        self._out_track.write(frame.data)

    async def interrupt(self) -> None:
        self._out_track.clear()

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        for task in self._tasks:
            task.cancel()
        if self.call_id:
            with contextlib.suppress(Exception):
                await self._wa.terminate_call(self.call_id)
        with contextlib.suppress(Exception):
            await self._pc.close()
