"""Offline unit tests for BaseVoiceAgentProxy hangup/connect helpers.

Run:  uv run tests/unit/test_proxy_drain.py

Covers wait_until_drained (don't clip the closing line before hanging up)
and wait_connected (hold back the first turn until the provider sockets are
up) using the no-network MockProxy — no API keys, no real sockets.
"""

from __future__ import annotations

import asyncio
import sys
import time
from typing import Any

from voiceagent.models import SessionConfig, SessionState
from voiceagent.providers.mock import MockProxy


class _NullTransport:
    """Satisfies AudioTransport; these tests drive the queue directly."""

    async def recv_frames(self):  # type: ignore[no-untyped-def]
        return
        yield  # pragma: no cover - makes this an async generator, never iterated

    async def send_frame(self, frame: Any) -> None:
        pass

    async def interrupt(self) -> None:
        pass

    async def close(self) -> None:
        pass


def _proxy() -> MockProxy:
    return MockProxy(SessionConfig(), _NullTransport())


async def main() -> int:
    failures: list[str] = []

    def check(name: str, ok: bool) -> None:
        print(f"  [{'PASS' if ok else 'FAIL'}] {name}")
        if not ok:
            failures.append(name)

    # -- drain waits for queued audio to leave, then a short tail -------------
    proxy = _proxy()
    proxy.enqueue_audio(b"\x00" * 320)  # one queued closing-line frame
    proxy.set_state(SessionState.SPEAKING)

    async def _drain_soon() -> None:
        await asyncio.sleep(0.15)  # downlink flushes and speaking ends
        while proxy._audio_out.qsize():
            proxy._audio_out.get_nowait()
        proxy.set_state(SessionState.LISTENING)

    t0 = time.monotonic()
    await asyncio.gather(
        proxy.wait_until_drained(start_timeout_s=1.0, max_wait_s=2.0, tail_s=0.1),
        _drain_soon(),
    )
    elapsed = time.monotonic() - t0
    check("drain waits for the queue to empty (>=0.15s)", elapsed >= 0.15)
    check("drain returns soon after draining (<0.6s)", elapsed < 0.6)

    # -- drain is time-capped if playback never ends -------------------------
    proxy = _proxy()
    proxy.enqueue_audio(b"\x00" * 320)
    proxy.set_state(SessionState.SPEAKING)  # never leaves SPEAKING
    t0 = time.monotonic()
    await proxy.wait_until_drained(start_timeout_s=0.2, max_wait_s=0.3, tail_s=0.05)
    check("drain is time-capped when playback never ends (<0.7s)",
          time.monotonic() - t0 < 0.7)

    # -- phase 1: hangup asked mid-turn waits for the line to BEGIN ----------
    proxy = _proxy()  # queue empty, state LISTENING (line not synthesized yet)

    async def _speak_soon() -> None:
        await asyncio.sleep(0.15)
        proxy.set_state(SessionState.SPEAKING)
        proxy.enqueue_audio(b"\x00" * 320)
        await asyncio.sleep(0.1)
        proxy._audio_out.get_nowait()
        proxy.set_state(SessionState.LISTENING)

    t0 = time.monotonic()
    await asyncio.gather(
        proxy.wait_until_drained(start_timeout_s=1.0, max_wait_s=2.0, tail_s=0.05),
        _speak_soon(),
    )
    check("drain waits for a mid-turn closing line to start then finish (>=0.25s)",
          time.monotonic() - t0 >= 0.25)

    # -- wait_connected: False before connect, True once flagged -------------
    proxy = _proxy()
    check("wait_connected times out before connect (False)",
          await proxy.wait_connected(timeout_s=0.1) is False)
    proxy._connected.set()
    check("wait_connected returns True once connected",
          await proxy.wait_connected(timeout_s=0.1) is True)

    print(f"\n{'ALL PASS' if not failures else f'{len(failures)} FAILURE(S): {failures}'}")
    return 0 if not failures else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))


# -- pytest adapter -----------------------------------------------------------
def test_suite() -> None:
    assert asyncio.run(main()) == 0
