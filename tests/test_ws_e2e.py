"""End-to-end WebSocket transport test (no API keys, real sockets).

Run:  uv run tests/test_ws_e2e.py

Starts the actual VoiceAgent WS server with the mock engine, connects a
real websockets client, and verifies:
  1. per-connection config overrides via query params
  2. binary uplink frames reach the engine
  3. spoken audio comes back as binary PCM frames
  4. barge-in pushes a {"type": "clear"} text message to the client
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import sys

import websockets

from voiceagent import VoiceAgent

PORT = 8901
URL = f"ws://127.0.0.1:{PORT}/?language=hi-IN&tone=warm"


async def main() -> int:
    failures: list[str] = []

    def check(name: str, ok: bool) -> None:
        print(f"  [{'PASS' if ok else 'FAIL'}] {name}")
        if not ok:
            failures.append(name)

    va = VoiceAgent(provider="mock")
    # Capture the per-connection proxy so the test can drive the mock script
    # (a real engine would get transcripts from provider ASR instead).
    captured: dict[str, object] = {}
    original = va._make_session

    def capturing(transport, overrides=None):
        proxy, bridge = original(transport, overrides)
        captured["proxy"] = proxy
        return proxy, bridge

    va._make_session = capturing  # type: ignore[method-assign]

    server = asyncio.create_task(va.arun(f"ws://127.0.0.1:{PORT}"))
    await asyncio.sleep(0.5)  # let the server bind

    async with websockets.connect(URL) as client:
        # 1) uplink: 25 binary PCM16 frames (20 ms @16 kHz)
        frame = b"\x00" * 640
        for _ in range(25):
            await client.send(frame)
        await asyncio.sleep(0.3)

        proxy = captured.get("proxy")
        check("session created on connect", proxy is not None)
        assert proxy is not None
        check(
            "query-param override applied (language=hi-IN)",
            proxy.config.language == "hi-IN" and proxy.config.tone == "warm",
        )
        check(
            f"uplink frames reached engine (got {proxy.frames_received})",
            proxy.frames_received >= 25,
        )

        # 2) downlink: make the agent speak, expect binary audio back
        proxy.push_user_utterance("hello there")
        got_audio = False
        got_clear = False
        try:
            async with asyncio.timeout(3.0):
                while not got_audio:
                    msg = await client.recv()
                    if isinstance(msg, bytes) and len(msg) > 0:
                        got_audio = True
        except TimeoutError:
            pass
        check("binary audio frames received by client", got_audio)

        # 3) barge-in: queue a long utterance, interrupt, expect "clear"
        await proxy.speak_text(
            "a very long sentence that will definitely still be in the queue"
        )
        await proxy.on_user_speech_started()
        try:
            async with asyncio.timeout(3.0):
                while not got_clear:
                    msg = await client.recv()
                    if isinstance(msg, str) and json.loads(msg).get("type") == "clear":
                        got_clear = True
        except TimeoutError:
            pass
        check('barge-in "clear" control message received', got_clear)

    server.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await server

    print(f"\n{'ALL PASS' if not failures else f'{len(failures)} FAILURE(S): {failures}'}")
    return 0 if not failures else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
