"""Bridge a Meta WhatsApp WebRTC leg (aiortc) into a LiveKit room.

This is the only place aiortc and livekit.rtc meet. The agent runs as an
ordinary worker participant in the room and never learns it's on WhatsApp;
this bridge just moves audio between the two peer connections.

    inbound  : Meta 48 kHz Opus -> aiortc decode -> 48 kHz s16 mono ->
               rtc.AudioSource.capture_frame  (no rate change — "passthrough")
    outbound : agent track -> rtc.AudioStream (48 kHz s16 mono) ->
               OutboundAudioTrack pacer -> aiortc sender (Opus)
    barge-in : the worker publishes b"clear" on the va.control data topic when
               the agent stops speaking -> flush the outbound buffer.

SDP is produced raw here; the WhatsApp channel munges it for Meta (keeping the
Meta-specific validator quirks in the channel layer, where they're tested).
"""

from __future__ import annotations

import asyncio
import contextlib
import fractions
import logging
import threading
import time

import av
from aiortc import RTCPeerConnection, RTCSessionDescription
from aiortc.mediastreams import MediaStreamTrack
from livekit import rtc

logger = logging.getLogger("voiceagent")

_WIRE_RATE = 48_000  # Opus native; also the room bridge rate (no resampling)
_FRAME_MS = 20
# Barge-in protocol — MUST match voiceagent.livekit.worker (the producer side).
BARGE_IN_TOPIC = "va.control"
BARGE_IN_CLEAR = b"clear"
# Cap the outbound buffer so playout latency can never accumulate past ~240 ms:
# on overflow we drop the OLDEST audio (a brief skip) rather than fall behind.
_MAX_BUFFER_MS = 240


class OutboundAudioTrack(MediaStreamTrack):
    """Pulls engine PCM from a buffer, emits paced 20 ms wire-rate frames.

    The buffer is lock-guarded because ``write`` (event loop) and ``recv``
    (aiortc's sender task) race on it; ``clear`` is barge-in. Pacing logic is
    the production-proven port from `main`.
    """

    kind = "audio"

    def __init__(self, engine_rate: int = _WIRE_RATE) -> None:
        super().__init__()
        self._engine_rate = engine_rate
        self._samples_per_frame = int(engine_rate * _FRAME_MS / 1000)
        self._max_bytes = int(engine_rate * _MAX_BUFFER_MS / 1000) * 2  # s16 mono
        self._buffer = bytearray()
        self._lock = threading.Lock()
        self._resampler = av.AudioResampler(format="s16", layout="mono", rate=_WIRE_RATE)
        self._pts = 0  # in wire-rate samples
        self._start: float | None = None

    def write(self, pcm: bytes) -> None:
        with self._lock:
            self._buffer.extend(pcm)
            overflow = len(self._buffer) - self._max_bytes
            if overflow > 0:
                del self._buffer[:overflow]  # drop oldest — never accumulate latency

    def clear(self) -> None:
        with self._lock:
            self._buffer.clear()

    async def recv(self) -> av.AudioFrame:
        # Wall-clock pacing: one frame per 20 ms, silence on underrun. Never
        # block on the engine — a stalled TTS must not stall RTP timing.
        if self._start is None:
            self._start = time.monotonic()
        target = self._start + self._pts / _WIRE_RATE
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
        out.sample_rate = _WIRE_RATE
        out.time_base = fractions.Fraction(1, _WIRE_RATE)
        self._pts += int(_WIRE_RATE * _FRAME_MS / 1000)
        return out


class RoomCallBridge:
    """Media bridge between an aiortc PeerConnection (Meta) and a LiveKit room.

    Lifecycle (outbound): connect_room -> create_offer -> [channel munges +
    Meta answers] -> set_answer. Inbound swaps the SDP steps: connect_room ->
    create_answer(offer) -> [channel pre_accepts/accepts]. Either way media
    flows once the remote description is set.
    """

    def __init__(self) -> None:
        self._pc = RTCPeerConnection()
        self._out_track = OutboundAudioTrack()
        self._source = rtc.AudioSource(_WIRE_RATE, 1)
        self._in_resampler = av.AudioResampler(format="s16", layout="mono", rate=_WIRE_RATE)
        self._room: rtc.Room | None = None
        self._tasks: list[asyncio.Task[None]] = []
        self._closed = False
        self._pc.addTrack(self._out_track)
        self._pc.on("track", self._on_meta_track)

    # -- SDP (raw; the WhatsApp channel munges before hitting Meta) ------------

    async def create_offer(self) -> str:
        offer = await self._pc.createOffer()
        await self._pc.setLocalDescription(offer)  # completes ICE gathering
        return self._pc.localDescription.sdp

    async def set_answer(self, sdp: str) -> None:
        await self._pc.setRemoteDescription(RTCSessionDescription(sdp=sdp, type="answer"))

    async def create_answer(self, offer_sdp: str) -> str:
        await self._pc.setRemoteDescription(RTCSessionDescription(sdp=offer_sdp, type="offer"))
        answer = await self._pc.createAnswer()
        await self._pc.setLocalDescription(answer)  # completes ICE gathering
        return self._pc.localDescription.sdp

    # -- LiveKit room ---------------------------------------------------------

    async def connect_room(self, url: str, token: str) -> None:
        room = rtc.Room()
        room.on("track_subscribed", self._on_agent_track)
        room.on("data_received", self._on_data)
        await room.connect(url, token)
        track = rtc.LocalAudioTrack.create_audio_track("wa-caller", self._source)
        opts = rtc.TrackPublishOptions()
        opts.source = rtc.TrackSource.SOURCE_MICROPHONE  # RoomIO only accepts mic
        await room.local_participant.publish_track(track, opts)
        self._room = room

    def _on_agent_track(
        self, track: rtc.Track, pub: rtc.RemoteTrackPublication, participant: rtc.RemoteParticipant
    ) -> None:
        if track.kind == rtc.TrackKind.KIND_AUDIO:
            self._tasks.append(asyncio.create_task(self._pump_room_to_meta(track)))

    def _on_data(self, packet: rtc.DataPacket) -> None:
        if packet.topic == BARGE_IN_TOPIC and packet.data == BARGE_IN_CLEAR:
            self._out_track.clear()

    async def _pump_room_to_meta(self, track: rtc.Track) -> None:
        stream = rtc.AudioStream(track, sample_rate=_WIRE_RATE, num_channels=1)
        try:
            async for event in stream:
                self._out_track.write(bytes(event.frame.data))
        except Exception as exc:  # stream closes on unsubscribe/disconnect
            logger.debug("room->meta pump ended: %r", exc)
        finally:
            await stream.aclose()

    # -- aiortc inbound (Meta -> room) ----------------------------------------

    def _on_meta_track(self, track: MediaStreamTrack) -> None:
        if track.kind == "audio":
            self._tasks.append(asyncio.create_task(self._pump_meta_to_room(track)))

    async def _pump_meta_to_room(self, track: MediaStreamTrack) -> None:
        try:
            while True:
                frame = await track.recv()
                resampled = self._in_resampler.resample(frame)
                for out in resampled if isinstance(resampled, list) else [resampled]:
                    pcm = bytes(out.planes[0])[: out.samples * 2]  # s16 mono, drop padding
                    await self._source.capture_frame(
                        rtc.AudioFrame(
                            data=pcm,
                            sample_rate=_WIRE_RATE,
                            num_channels=1,
                            samples_per_channel=out.samples,
                        )
                    )
        except Exception as exc:
            logger.debug("meta->room pump ended: %r", exc)

    # -- teardown -------------------------------------------------------------

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        for task in self._tasks:
            task.cancel()
        if self._room is not None:
            with contextlib.suppress(Exception):
                await self._room.disconnect()
        with contextlib.suppress(Exception):
            await self._pc.close()
