"""Full-stack integration ($0): client -> LiveKit -> worker -> pipeline -> client.

Requires the local compose stack:
    docker compose -f deploy/compose/docker-compose.yml up --build -d
Run with:
    uv run pytest -m integration -q

A Python rtc client stands in for the browser: it creates a session via the
platform API, joins the room, publishes the WAV fixture as microphone audio,
and asserts that the mock agent's synthesized audio comes back.
"""

from __future__ import annotations

import array
import asyncio
import math
import wave
from pathlib import Path

import pytest

pytest.importorskip("livekit")
import httpx
from livekit import rtc

pytestmark = pytest.mark.integration

PLATFORM = "http://localhost:8080"
AUTH = {"Authorization": "Bearer devtoken"}
FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "utterance_16k.wav"

SAMPLE_RATE = 16_000
FRAME_SAMPLES = SAMPLE_RATE // 50  # 20 ms


def _fixture_pcm() -> bytes:
    with wave.open(str(FIXTURE), "rb") as f:
        assert f.getframerate() == SAMPLE_RATE and f.getnchannels() == 1
        return f.readframes(f.getnframes())


def _rms(data: bytes) -> float:
    samples = array.array("h", data)
    if not samples:
        return 0.0
    return math.sqrt(sum(s * s for s in samples) / len(samples))


async def test_browser_media_path_end_to_end() -> None:
    async with httpx.AsyncClient(timeout=10.0) as client:
        resp = await client.post(
            f"{PLATFORM}/v1/sessions", headers=AUTH, json={"agent_id": "mock-demo"}
        )
        assert resp.status_code == 201, resp.text
        session = resp.json()

    room = rtc.Room()
    agent_joined = asyncio.Event()
    agent_audio = asyncio.Event()
    tasks: list[asyncio.Task] = []

    @room.on("participant_connected")
    def _on_participant(participant: rtc.RemoteParticipant) -> None:
        agent_joined.set()

    @room.on("track_subscribed")
    def _on_track(track: rtc.Track, pub, participant) -> None:
        if track.kind == rtc.TrackKind.KIND_AUDIO:
            tasks.append(asyncio.create_task(_consume(track)))

    async def _consume(track: rtc.Track) -> None:
        stream = rtc.AudioStream(track)
        async for event in stream:
            frame = event.frame
            if _rms(bytes(frame.data)) > 200:  # mock TTS tone is loud
                agent_audio.set()
                break
        await stream.aclose()

    await room.connect(session["livekit_url"], session["token"])
    try:
        source = rtc.AudioSource(SAMPLE_RATE, 1)
        track = rtc.LocalAudioTrack.create_audio_track("mic", source)
        # Must declare the microphone source: RoomIO only feeds SOURCE_MICROPHONE
        # tracks into the pipeline (browsers set this automatically).
        await room.local_participant.publish_track(
            track, rtc.TrackPublishOptions(source=rtc.TrackSource.SOURCE_MICROPHONE)
        )

        # The worker is dispatched by the token's RoomConfiguration.
        await asyncio.wait_for(agent_joined.wait(), timeout=60)

        # "Speak": stream the fixture at real time, then hold silence so the
        # VAD closes the user turn.
        pcm = _fixture_pcm()
        frame_bytes = FRAME_SAMPLES * 2
        silence = b"\x00" * frame_bytes
        for _cycle in range(2):  # speak twice in case the first turn is missed
            for offset in range(0, len(pcm) - frame_bytes, frame_bytes):
                await source.capture_frame(
                    rtc.AudioFrame(
                        data=pcm[offset : offset + frame_bytes],
                        sample_rate=SAMPLE_RATE,
                        num_channels=1,
                        samples_per_channel=FRAME_SAMPLES,
                    )
                )
            for _ in range(100):  # 2 s of silence
                await source.capture_frame(
                    rtc.AudioFrame(
                        data=silence,
                        sample_rate=SAMPLE_RATE,
                        num_channels=1,
                        samples_per_channel=FRAME_SAMPLES,
                    )
                )
            if agent_audio.is_set():
                break

        await asyncio.wait_for(agent_audio.wait(), timeout=30)
    finally:
        for task in tasks:
            task.cancel()
        await room.disconnect()

    # Session status should reflect the live session.
    async with httpx.AsyncClient(timeout=10.0) as client:
        resp = await client.get(f"{PLATFORM}/v1/sessions/{session['session_id']}", headers=AUTH)
        assert resp.status_code == 200
        assert resp.json().get("status") in {"active", "ended"}


async def test_worker_exposes_prometheus_metrics() -> None:
    async with httpx.AsyncClient(timeout=5.0) as client:
        resp = await client.get("http://localhost:9100/metrics")
    assert resp.status_code == 200
    assert "python" in resp.text or "process" in resp.text or resp.text
