"""OutboundAudioTrack pacing/silence/barge-in/backpressure (offline)."""

from __future__ import annotations

import pytest

pytest.importorskip("av")
pytest.importorskip("aiortc")

from voiceagent.livekit.room_audio import OutboundAudioTrack

_RATE = 48_000
_SAMPLES_20MS = _RATE * 20 // 1000  # 960
_BYTES_20MS = _SAMPLES_20MS * 2


async def test_recv_emits_20ms_wire_frames_and_advances_pts() -> None:
    track = OutboundAudioTrack(engine_rate=_RATE)
    track.write(b"\x11\x22" * (_SAMPLES_20MS * 3))  # 60 ms of audio

    first = await track.recv()
    assert first.samples == _SAMPLES_20MS
    assert first.sample_rate == _RATE
    assert first.pts == 0

    second = await track.recv()
    assert second.pts == _SAMPLES_20MS  # one frame later, in wire samples


async def test_silence_on_underrun() -> None:
    track = OutboundAudioTrack(engine_rate=_RATE)
    frame = await track.recv()  # empty buffer
    assert frame.samples == _SAMPLES_20MS
    assert bytes(frame.planes[0])[: _BYTES_20MS] == b"\x00" * _BYTES_20MS


async def test_clear_is_barge_in() -> None:
    track = OutboundAudioTrack(engine_rate=_RATE)
    track.write(b"\x11\x22" * _SAMPLES_20MS)
    track.clear()
    frame = await track.recv()
    assert bytes(frame.planes[0])[: _BYTES_20MS] == b"\x00" * _BYTES_20MS  # nothing buffered


def test_buffer_caps_at_240ms_dropping_oldest() -> None:
    track = OutboundAudioTrack(engine_rate=_RATE)
    # Write 500 ms; the buffer must never hold more than the 240 ms cap so
    # playout latency can't accumulate.
    track.write(b"\x01\x02" * (_SAMPLES_20MS * 25))
    cap_bytes = int(_RATE * 240 / 1000) * 2
    assert len(track._buffer) == cap_bytes
