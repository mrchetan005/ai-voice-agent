#!/usr/bin/env python3
"""Load-test driver: N concurrent sessions vs a (mock) agent.

Measures the metric that actually sizes a worker fleet — time-to-first-agent-
audio under concurrency — plus success rate, then optionally scrapes a worker's
Prometheus endpoint for its self-reported load. Divide sessions by observed
per-worker load to get sessions-per-worker; size `worker.autoscaling` from that
(see deploy/k8s/README.md), rather than guessing from CPU.

Run against the compose stack (mock agent = $0) or a real cluster:
    uv run python deploy/loadtest/driver.py \
        --platform http://localhost:8080 --token devtoken \
        --agent mock-demo --sessions 20 --ramp 5 --hold 10

Requires the `agents`/`platform` extras (livekit, httpx). Mirrors the proven
raw-rtc client from tests/integration/test_browser_path.py.
"""

from __future__ import annotations

import argparse
import array
import asyncio
import contextlib
import math
import time
import wave
from dataclasses import dataclass
from pathlib import Path

import httpx
from livekit import rtc

_FIXTURE = Path(__file__).resolve().parents[2] / "tests" / "fixtures" / "utterance_16k.wav"
_SAMPLE_RATE = 16_000
_FRAME_SAMPLES = _SAMPLE_RATE // 50  # 20 ms


def _fixture_pcm() -> bytes:
    with wave.open(str(_FIXTURE), "rb") as f:
        if f.getframerate() != _SAMPLE_RATE or f.getnchannels() != 1:
            raise SystemExit(f"fixture must be {_SAMPLE_RATE} Hz mono")
        return f.readframes(f.getnframes())


def _rms(data: bytes) -> float:
    samples = array.array("h", data)
    return math.sqrt(sum(s * s for s in samples) / len(samples)) if samples else 0.0


def _percentile(values: list[float], pct: float) -> float:
    if not values:
        return float("nan")
    ordered = sorted(values)
    rank = max(0, math.ceil(pct / 100 * len(ordered)) - 1)
    return ordered[rank]


@dataclass
class Result:
    ok: bool
    ttfa_s: float | None  # time-to-first-agent-audio
    error: str = ""


async def _one_session(
    client: httpx.AsyncClient, args: argparse.Namespace, pcm: bytes
) -> Result:
    t0 = time.monotonic()
    try:
        resp = await client.post(
            f"{args.platform}/v1/sessions",
            headers={"Authorization": f"Bearer {args.token}"},
            json={"agent_id": args.agent},
            timeout=15.0,
        )
        if resp.status_code != 201:
            return Result(False, None, f"create {resp.status_code}: {resp.text[:120]}")
        session = resp.json()
    except Exception as exc:
        return Result(False, None, f"create failed: {exc!r}")

    room = rtc.Room()
    agent_audio = asyncio.Event()
    agent_joined = asyncio.Event()
    heard_at: dict[str, float] = {}
    consumers: list[asyncio.Task[None]] = []

    @room.on("participant_connected")
    def _on_participant(participant) -> None:  # the agent dispatched into the room
        agent_joined.set()

    @room.on("track_subscribed")
    def _on_track(track: rtc.Track, pub, participant) -> None:
        if track.kind == rtc.TrackKind.KIND_AUDIO:
            consumers.append(asyncio.create_task(_consume(track)))

    async def _consume(track: rtc.Track) -> None:
        stream = rtc.AudioStream(track)
        try:
            async for event in stream:
                if _rms(bytes(event.frame.data)) > 200:
                    heard_at["t"] = time.monotonic()
                    agent_audio.set()
                    break
        finally:
            await stream.aclose()

    speaker: asyncio.Task[None] | None = None
    try:
        await room.connect(session["livekit_url"], session["token"])
        source = rtc.AudioSource(_SAMPLE_RATE, 1)
        track = rtc.LocalAudioTrack.create_audio_track("mic", source)
        await room.local_participant.publish_track(
            track, rtc.TrackPublishOptions(source=rtc.TrackSource.SOURCE_MICROPHONE)
        )
        # Don't speak into an empty room: wait for the agent to be dispatched in,
        # else the opening utterance is lost and STT never hears the user.
        if not room.remote_participants:
            try:
                await asyncio.wait_for(agent_joined.wait(), timeout=args.timeout)
            except TimeoutError:
                return Result(False, None, "agent never joined room")
        hard_deadline = time.monotonic() + args.timeout
        speaker = asyncio.create_task(
            _speak(source, pcm, agent_audio, args.hold, hard_deadline)
        )
        try:
            await asyncio.wait_for(
                agent_audio.wait(), timeout=max(0.0, hard_deadline - time.monotonic())
            )
        except TimeoutError:
            return Result(False, None, "no agent audio before timeout")
        ttfa = heard_at["t"] - t0
        # Sustain the call for the rest of the hold window (keeps N sessions
        # genuinely concurrent); _speak returns on its own once hold elapses.
        with contextlib.suppress(asyncio.CancelledError):
            await speaker
        return Result(True, ttfa)
    except Exception as exc:
        return Result(False, None, f"session failed: {exc!r}")
    finally:
        if speaker is not None:
            speaker.cancel()
        for c in consumers:
            c.cancel()
        await room.disconnect()


async def _speak(
    source: rtc.AudioSource,
    pcm: bytes,
    agent_audio: asyncio.Event,
    hold_s: float,
    hard_deadline: float,
) -> None:
    """Loop the fixture (speak + 2 s silence to close each turn), sustaining the
    call for at least `hold_s` and until the agent replies — but never past
    `hard_deadline`, so a mute agent still lets the caller time out."""
    frame_bytes = _FRAME_SAMPLES * 2
    silence = b"\x00" * frame_bytes
    hold_deadline = time.monotonic() + hold_s
    while time.monotonic() < hard_deadline:
        if agent_audio.is_set() and time.monotonic() >= hold_deadline:
            return
        for offset in range(0, len(pcm) - frame_bytes, frame_bytes):
            await source.capture_frame(
                rtc.AudioFrame(
                    data=pcm[offset : offset + frame_bytes],
                    sample_rate=_SAMPLE_RATE,
                    num_channels=1,
                    samples_per_channel=_FRAME_SAMPLES,
                )
            )
        for _ in range(100):  # 2 s silence -> closes the user turn
            await source.capture_frame(
                rtc.AudioFrame(
                    data=silence,
                    sample_rate=_SAMPLE_RATE,
                    num_channels=1,
                    samples_per_channel=_FRAME_SAMPLES,
                )
            )


async def _scrape_worker_load(metrics_url: str) -> str:
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.get(metrics_url)
        lines = [
            ln
            for ln in resp.text.splitlines()
            if ln.startswith("lk_agents_") and ("load" in ln or "job" in ln)
        ]
        return "\n    ".join(lines[:10]) or "(no lk_agents_* load/job metrics found)"
    except Exception as exc:
        return f"(scrape failed: {exc!r})"


async def _run(args: argparse.Namespace) -> int:
    pcm = _fixture_pcm()
    results: list[Result] = []

    async with httpx.AsyncClient() as client:

        async def _staggered(i: int) -> None:
            if args.ramp:
                await asyncio.sleep(i * args.ramp / max(1, args.sessions))
            results.append(await _one_session(client, args, pcm))

        await asyncio.gather(*(_staggered(i) for i in range(args.sessions)))

    ok = [r for r in results if r.ok]
    ttfas = [r.ttfa_s for r in ok if r.ttfa_s is not None]
    pct = 100 * len(ok) / args.sessions if args.sessions else 0.0
    print(f"\n=== loadtest: {args.sessions} sessions vs {args.agent!r} ===")
    print(f"success:   {len(ok)}/{args.sessions} ({pct:.0f}%)")
    if ttfas:
        print(f"ttfa p50:  {_percentile(ttfas, 50):.2f}s")
        print(f"ttfa p95:  {_percentile(ttfas, 95):.2f}s")
        print(f"ttfa max:  {max(ttfas):.2f}s")
    for r in results:
        if not r.ok:
            print(f"  FAIL: {r.error}")
    if args.metrics_url:
        print(f"\nworker metrics ({args.metrics_url}):")
        print("    " + await _scrape_worker_load(args.metrics_url))
    return 0 if len(ok) == args.sessions else 1


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--platform", default="http://localhost:8080")
    p.add_argument("--token", default="devtoken", help="PLATFORM_API_TOKEN")
    p.add_argument("--agent", default="mock-demo")
    p.add_argument("--sessions", type=int, default=10)
    p.add_argument("--ramp", type=float, default=0.0, help="seconds to spread session starts over")
    p.add_argument("--hold", type=float, default=10.0, help="seconds to keep each call speaking")
    p.add_argument("--timeout", type=float, default=60.0, help="per-session first-audio timeout")
    p.add_argument("--metrics-url", default="", help="worker /metrics to scrape after the run")
    args = p.parse_args()
    raise SystemExit(asyncio.run(_run(args)))


if __name__ == "__main__":
    main()
