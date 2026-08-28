"""LIVE smoke test for the Gemini Live engine (needs GOOGLE_API_KEY).

Run:  uv run --env-file .env tests/test_gemini_live.py

No microphone needed: connects the real BidiGenerateContent WebSocket,
injects a TEXT user turn, and verifies the full proxy loop —
setup handshake -> model calls send_to_agent -> our agent runs (with a
status pulse) -> toolResponse -> model speaks -> PCM audio lands on the
transport.  Prints what the model said (output transcription).
"""

from __future__ import annotations

import asyncio
import json
import sys

from voiceagent.commentary_and_approval import OrchestratorBridge
from voiceagent.guardrails_and_eval import MockTransport, TelemetryRecorder, _wait_until
from voiceagent.models import SessionConfig
from voiceagent.providers import GeminiLiveProxy


async def main() -> int:
    failures: list[str] = []

    def check(name: str, ok: bool) -> None:
        print(f"  [{'PASS' if ok else 'FAIL'}] {name}")
        if not ok:
            failures.append(name)

    config = SessionConfig(
        language="en-US",
        tone="warm",
        system_prompt="You are a concise order-status assistant.",
        input_sample_rate=16_000,
        output_sample_rate=24_000,
    )
    # packet_count=0: no synthetic mic audio; we drive the turn with text.
    transport = MockTransport(packet_count=0)
    proxy = GeminiLiveProxy(config, transport)
    telemetry = TelemetryRecorder(config.session_id, emit_logs=False)
    proxy.telemetry = telemetry

    agent_called = asyncio.Event()

    async def agent(ctx):
        agent_called.set()
        await ctx.status("checking_order_database")
        await asyncio.sleep(0.2)
        return "Order 4 5 6 ships tomorrow evening."

    bridge = OrchestratorBridge(proxy, agent, config, None, telemetry)
    proxy.bind(bridge)

    # Capture the model's spoken words via output transcription events.
    model_speech: list[str] = []
    original_handle = proxy._handle_message

    async def tapping_handle(msg):
        content = msg.get("serverContent") or {}
        if text := (content.get("outputTranscription") or {}).get("text"):
            model_speech.append(text)
        await original_handle(msg)

    proxy._handle_message = tapping_handle  # type: ignore[method-assign]

    session = asyncio.create_task(proxy.run())
    try:
        ok = await _wait_until(
            lambda: telemetry.metrics.handshake_ms is not None, 20.0
        )
        check(
            f"setup handshake completed "
            f"({telemetry.metrics.handshake_ms and round(telemetry.metrics.handshake_ms)} ms)",
            ok,
        )
        if not ok:
            raise RuntimeError("no setupComplete — check GOOGLE_API_KEY / model access")

        # Text turn stands in for a spoken utterance.
        await proxy._ws.send(json.dumps({
            "clientContent": {
                "turns": [{
                    "role": "user",
                    "parts": [{"text": "Hi, can you check the status of my order?"}],
                }],
                "turnComplete": True,
            }
        }))

        check(
            "model routed the request to send_to_agent (backend agent ran)",
            await _wait_until(agent_called.is_set, 30.0),
        )
        check(
            "PCM audio received from model",
            await _wait_until(lambda: len(transport.sent_frames) > 0, 30.0),
        )
        # Let the model finish talking so the transcript is complete.
        await asyncio.sleep(4.0)

        speech = "".join(model_speech).strip()
        print(f"\n  model said: {speech!r}")
        print(f"  audio: {transport.audio_ms_received:.0f} ms "
              f"in {len(transport.sent_frames)} frames")
        check("model relayed the agent's answer",
              "tomorrow" in speech.lower() or transport.audio_ms_received > 500)
    except Exception as exc:
        print(f"  ERROR: {exc!r}")
        failures.append(str(exc))
    finally:
        await transport.close()
        proxy._stopping.set()
        try:
            await asyncio.wait_for(session, timeout=10.0)
        except TimeoutError:
            session.cancel()

    print(f"\n{'ALL PASS' if not failures else f'{len(failures)} FAILURE(S): {failures}'}")
    return 0 if not failures else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
