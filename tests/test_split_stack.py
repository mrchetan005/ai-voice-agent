"""LIVE end-to-end latency check for the split stack:
Deepgram (STT) -> Groq (LLM) -> Cartesia (TTS).

Run:  uv run --env-file .env tests/test_split_stack.py [--voice rohan|kavita|both]

Needs DEEPGRAM_API_KEY, GROQ_API_KEY, CARTESIA_API_KEY. No microphone or
WhatsApp call: a WavTransport streams tests/fixtures/utterance_16k.wav at
realtime pacing (plus trailing silence so Deepgram endpointing fires), twice
per session — turn 1 is the cold turn (Groq TLS handshake), turn 2 is the
steady-state number. Deliverable is the printed latency table:

  asr_latency_ms    speech start -> final transcript (includes utterance)
  llm_ttfb_ms       Groq request -> first token (real TTFB)
  tts_first_byte_ms speak_token_stream start -> first Cartesia PCM
  e2e_response_ms   END of user utterance -> first reply audio (the number
                    a caller actually feels)

Kept deliberately cheap: replies capped at ~15 words, filler phrase-cache
warm disabled (it burns Cartesia characters and races turn-1 TTS).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import wave
from collections.abc import AsyncIterator
from pathlib import Path

from voiceagent.commentary_and_approval import OrchestratorBridge
from voiceagent.guardrails_and_eval import TelemetryRecorder, _wait_until
from voiceagent.models import AudioFrame, SessionConfig
from voiceagent.providers import SplitStackProxy, make_llm_agent

FIXTURE = Path(__file__).parent / "fixtures" / "utterance_16k.wav"

VOICES = {
    "rohan": "4877b818-c7fe-4c89-b1cf-eadf8e23da72",   # male
    "kavita": "56e35e2d-6eb6-4226-ab8b-9776515a7094",  # female
}

FRAME_MS = 20


class WavTransport:
    """Synthetic caller: plays the fixture N times at realtime pacing with a
    silence gap between plays, then holds the line open with silence."""

    def __init__(
        self,
        wav_path: Path,
        utterances: int = 2,
        gap_s: float = 10.0,
        sample_rate: int = 16_000,
    ) -> None:
        with wave.open(str(wav_path), "rb") as wav:
            assert wav.getframerate() == sample_rate, wav.getframerate()
            assert wav.getnchannels() == 1 and wav.getsampwidth() == 2
            self._pcm = wav.readframes(wav.getnframes())
        self._utterances = utterances
        self._gap_s = gap_s
        self.sample_rate = sample_rate
        self.sent_frames: list[AudioFrame] = []
        self.interrupts = 0
        self._closed = asyncio.Event()

    async def _silence(self, seconds: float) -> AsyncIterator[AudioFrame]:
        frame_bytes = int(self.sample_rate * FRAME_MS / 1000) * 2
        silence = b"\x00" * frame_bytes
        for _ in range(int(seconds * 1000 / FRAME_MS)):
            if self._closed.is_set():
                return
            await asyncio.sleep(FRAME_MS / 1000)
            yield AudioFrame(data=silence, sample_rate=self.sample_rate)

    async def recv_frames(self) -> AsyncIterator[AudioFrame]:
        frame_bytes = int(self.sample_rate * FRAME_MS / 1000) * 2
        async for frame in self._silence(0.5):  # let the session settle
            yield frame
        for _ in range(self._utterances):
            for offset in range(0, len(self._pcm), frame_bytes):
                if self._closed.is_set():
                    return
                await asyncio.sleep(FRAME_MS / 1000)
                yield AudioFrame(
                    data=self._pcm[offset:offset + frame_bytes],
                    sample_rate=self.sample_rate,
                )
            async for frame in self._silence(self._gap_s):
                yield frame
        while not self._closed.is_set():  # user went quiet but didn't hang up
            async for frame in self._silence(1.0):
                yield frame

    async def send_frame(self, frame: AudioFrame) -> None:
        self.sent_frames.append(frame)

    async def interrupt(self) -> None:
        self.interrupts += 1

    async def close(self) -> None:
        self._closed.set()

    @property
    def audio_ms_received(self) -> float:
        return sum(f.duration_ms for f in self.sent_frames)


async def run_session(voice_name: str, voice_id: str, failures: list[str]) -> None:
    def check(name: str, ok: bool) -> None:
        print(f"  [{'PASS' if ok else 'FAIL'}] {name}")
        if not ok:
            failures.append(f"{voice_name}: {name}")

    print(f"\n=== voice: {voice_name} ({voice_id}) ===")
    config = SessionConfig(
        language="en-IN",
        tone="warm",
        system_prompt=(
            "You are a scheduling assistant for a small clinic. "
            "Keep every reply under 15 words."
        ),
        voice_id=voice_id,
        input_sample_rate=16_000,
        output_sample_rate=24_000,
        provider_options={
            "asr": "deepgram",
            "tts": "cartesia",
            "llm_base_url": "https://api.groq.com/openai/v1",
            "llm_model": "openai/gpt-oss-20b",
            "llm_api_key_env": "GROQ_API_KEY",
        },
    )
    transport = WavTransport(FIXTURE, utterances=2, gap_s=10.0)
    proxy = SplitStackProxy(config, transport)
    telemetry = TelemetryRecorder(config.session_id, emit_logs=False)
    proxy.telemetry = telemetry

    # Skip filler warm-up: costs Cartesia characters and would race turn-1.
    async def no_warm(*args, **kwargs) -> int:
        return 0

    proxy.phrase_cache.warm = no_warm  # type: ignore[method-assign]

    agent = make_llm_agent(
        proxy.llm, on_first_token=lambda ms: telemetry.record("llm_ttfb", ms)
    )
    bridge = OrchestratorBridge(proxy, agent, config, None, telemetry)
    proxy.bind(bridge)

    transcripts: list[str] = []
    original_on_transcript = proxy.on_user_transcript

    async def tap_transcript(text: str) -> None:
        transcripts.append(text)
        print(f"  [transcript] {text!r}")
        await original_on_transcript(text)

    proxy.on_user_transcript = tap_transcript  # type: ignore[method-assign]

    metrics = telemetry.metrics
    session = asyncio.create_task(proxy.run())
    try:
        ok = await _wait_until(lambda: metrics.handshake_ms is not None, 20.0)
        check(
            f"ASR+TTS handshake ({metrics.handshake_ms and round(metrics.handshake_ms)} ms)",
            ok,
        )
        if not ok:
            raise RuntimeError("no handshake — check DEEPGRAM/CARTESIA keys")

        check(
            "turn 1: transcript received",
            await _wait_until(lambda: len(transcripts) >= 1, 30.0),
        )
        check(
            "turn 1: transcript mentions the appointment",
            any("appointment" in t.lower() for t in transcripts),
        )
        check(
            "turn 1: reply audio (PCM) received",
            await _wait_until(lambda: len(transport.sent_frames) > 0, 30.0),
        )
        check(
            "turn 2: transcript + reply audio",
            await _wait_until(lambda: len(metrics.e2e_response_ms) >= 2, 45.0),
        )
        await asyncio.sleep(2.0)  # let turn-2 audio finish arriving

        print(f"\n  latency (turn 1 = cold, turn 2 = warm), voice={voice_name}:")
        for label, values in (
            ("asr_latency_ms   (speech->final)", metrics.asr_latency_ms),
            ("llm_ttfb_ms      (Groq 1st token)", metrics.llm_ttfb_ms),
            ("tts_first_byte_ms (1st PCM)", metrics.tts_first_byte_ms),
            ("e2e_response_ms  (felt pause)", metrics.e2e_response_ms),
            ("agent_turn_ms    (full turn)", metrics.agent_turn_ms),
        ):
            print(f"    {label:36s} {[round(v) for v in values]}")
        print(f"    reply audio: {transport.audio_ms_received:.0f} ms "
              f"in {len(transport.sent_frames)} frames")

        if len(metrics.e2e_response_ms) >= 2:
            check(
                f"warm-turn e2e under 3000 ms ({metrics.e2e_response_ms[1]:.0f} ms)",
                metrics.e2e_response_ms[1] < 3000,
            )
    except Exception as exc:
        print(f"  ERROR: {exc!r}")
        failures.append(f"{voice_name}: {exc!r}")
    finally:
        await transport.close()
        proxy._stopping.set()
        try:
            await asyncio.wait_for(session, timeout=15.0)
        except TimeoutError:
            session.cancel()
        print("  full report:")
        print("  " + json.dumps(telemetry.report(), indent=2).replace("\n", "\n  "))


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--voice", choices=(*VOICES, "both"), default="both")
    args = parser.parse_args()

    if not FIXTURE.exists():
        print(f"missing fixture {FIXTURE} — see plan Phase 2 (SAPI one-liner)")
        return 1

    failures: list[str] = []
    names = list(VOICES) if args.voice == "both" else [args.voice]
    for name in names:
        await run_session(name, VOICES[name], failures)

    print(f"\n{'ALL PASS' if not failures else f'{len(failures)} FAILURE(S): {failures}'}")
    return 0 if not failures else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
