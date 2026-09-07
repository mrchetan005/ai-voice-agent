"""OFFLINE checks for concurrent calls: the capacity gate, the same-peer
guard, task-per-call inbound, the call:status walk and the _PgStore lock.
No network, no DB, no API keys — _run_call is stubbed.

Run:  uv run tests/unit/test_concurrent_calls.py
"""

from __future__ import annotations

import asyncio
import os
import sys
import time

import fakeredis.aioredis

from whatsapp_agent.channels.call_manager import CallBusy, CallManager
from whatsapp_agent.channels.events import CallEvent, EventRouter
from whatsapp_agent.infra.redis import RedisGateway
from whatsapp_agent.infra.runtime_config import RuntimeConfig
from whatsapp_agent.infra.stores import SessionStore


class FakeWA:
    def __init__(self) -> None:
        self.terminated: list[str] = []
        self.permission_requests: list[str] = []

    async def enable_calling(self) -> dict:
        return {}

    async def send_permission_request(self, to: str, text: str) -> dict:
        self.permission_requests.append(to)
        return {}

    async def terminate_call(self, call_id: str) -> dict:
        self.terminated.append(call_id)
        return {}


class Gate:
    """Stub _run_call: parks every call on one event, tracks concurrency."""

    def __init__(self) -> None:
        self.release = asyncio.Event()
        self.running = 0
        self.peak = 0

    async def run_call(self, spec, session) -> int:
        self.running += 1
        self.peak = max(self.peak, self.running)
        try:
            await self.release.wait()
            return 0
        finally:
            self.running -= 1


def _manager(redis: RedisGateway | None = None) -> tuple[CallManager, FakeWA]:
    wa = FakeWA()
    manager = CallManager(
        EventRouter(), wa, object(), SessionStore(""),
        redis=redis, config=RuntimeConfig(""),
    )
    return manager, wa


async def _drain(manager: CallManager) -> None:
    if manager._bg_tasks:
        await asyncio.gather(*list(manager._bg_tasks), return_exceptions=True)


async def main() -> int:
    failures: list[str] = []

    def check(name: str, ok: bool) -> None:
        print(f"  [{'PASS' if ok else 'FAIL'}] {name}")
        if not ok:
            failures.append(name)

    os.environ["MAX_CONCURRENT_CALLS"] = "3"
    try:
        # -- capacity gate: 3 in flight, 4th refused, slot freed on completion --
        manager, _ = _manager()
        gate = Gate()
        manager._run_call = gate.run_call
        refs = [
            await manager.start_outbound(peer=f"91900000000{i}", skip_permission=True)
            for i in (1, 2, 3)
        ]
        await asyncio.sleep(0.1)
        check("3 calls run concurrently under limit 3",
              gate.running == 3 and manager.active_call_count == 3
              and len(set(refs)) == 3)
        try:
            await manager.start_outbound(peer="919000000004", skip_permission=True)
            check("4th call refused at capacity", False)
        except CallBusy as exc:
            check("4th call refused at capacity", str(exc) == "at capacity")
        gate.release.set()
        await _drain(manager)
        check("slots freed after completion", manager.active_call_count == 0)
        ref = await manager.start_outbound(peer="919000000005", skip_permission=True)
        await _drain(manager)
        check("new call admitted once a slot is free", bool(ref))

        # -- same-peer guard holds even with Redis disabled (fail-open lock) ----
        manager2, _ = _manager()
        gate2 = Gate()
        manager2._run_call = gate2.run_call
        await manager2.start_outbound(peer="919000000042", skip_permission=True)
        await asyncio.sleep(0.05)
        try:
            await manager2.run_outbound(peer="919000000042", skip_permission=True)
            check("same-peer second call raises CallBusy", False)
        except CallBusy as exc:
            check("same-peer second call raises CallBusy",
                  str(exc) == "919000000042")
        gate2.release.set()
        await _drain(manager2)

        # -- inbound at capacity: ringing leg terminated, nothing opened --------
        os.environ["MAX_CONCURRENT_CALLS"] = "0"
        manager3, wa3 = _manager()
        loop_task = asyncio.create_task(manager3._inbound_loop())
        manager3.router.incoming_calls.put_nowait(
            CallEvent(call_id="c-in-1", sdp="v=0", sdp_type="offer",
                      from_number="9111"))
        await asyncio.sleep(0.1)
        check("inbound at capacity is terminated, no session/slot",
              wa3.terminated == ["c-in-1"]
              and not manager3.router._sessions
              and manager3.active_call_count == 0)
        loop_task.cancel()

        # -- two inbound calls from different peers run simultaneously ----------
        os.environ["MAX_CONCURRENT_CALLS"] = "2"
        manager4, _ = _manager()
        gate4 = Gate()
        manager4._run_call = gate4.run_call
        loop_task = asyncio.create_task(manager4._inbound_loop())
        for i in (1, 2):
            manager4.router.incoming_calls.put_nowait(
                CallEvent(call_id=f"c-in-{i}", sdp="v=0", sdp_type="offer",
                          from_number=f"911100000{i}"))
        await asyncio.sleep(0.1)
        check("two inbound calls in flight at once",
              gate4.peak == 2 and manager4.active_call_count == 2)
        gate4.release.set()
        await _drain(manager4)
        check("inbound sessions closed and slots freed",
              not manager4.router._sessions and manager4.active_call_count == 0)
        loop_task.cancel()

        # -- limit change (env/runtime config) honored on the NEXT start --------
        os.environ["MAX_CONCURRENT_CALLS"] = "1"
        manager5, _ = _manager()
        gate5 = Gate()
        manager5._run_call = gate5.run_call
        await manager5.start_outbound(peer="919000000051", skip_permission=True)
        await asyncio.sleep(0.05)
        try:
            await manager5.start_outbound(peer="919000000052", skip_permission=True)
            second_refused = False
        except CallBusy:
            second_refused = True
        os.environ["MAX_CONCURRENT_CALLS"] = "2"
        ref = await manager5.start_outbound(peer="919000000052", skip_permission=True)
        check("limit change honored without restart", second_refused and bool(ref))
        gate5.release.set()
        await _drain(manager5)

        # -- call:status walk (fakeredis) ----------------------------------------
        gw = RedisGateway(None)
        gw._redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
        manager6, _ = _manager(redis=gw)

        async def instant_call(spec, session) -> int:
            return 0

        manager6._run_call = instant_call
        recorded: list[str] = []
        original_set_status = manager6._set_status

        async def recording_set_status(call_ref, status, peer="", reason=""):
            recorded.append(status)
            await original_set_status(call_ref, status, peer, reason)

        manager6._set_status = recording_set_status
        ref = await manager6.start_outbound(peer="919000000061", skip_permission=True)
        await _drain(manager6)
        final = await gw.get_json(f"call:status:{ref}")
        check("happy path walks accepted -> dialing -> completed",
              recorded == ["accepted", "dialing", "completed"]
              and final and final["status"] == "completed")

        # -- permission timeout writes permission_denied -------------------------
        from whatsapp_agent.channels import events as events_mod

        original_wait = events_mod.CallSession.wait_permission

        async def instant_timeout(self, timeout_s: float = 300.0) -> bool:
            raise TimeoutError

        events_mod.CallSession.wait_permission = instant_timeout
        try:
            manager7, _ = _manager(redis=gw)
            manager7._run_call = instant_call
            ref = await manager7.start_outbound(peer="919000000071")
            await _drain(manager7)
            final = await gw.get_json(f"call:status:{ref}")
            check("permission timeout -> permission_denied (not completed)",
                  final and final["status"] == "permission_denied"
                  and final.get("reason") == "timeout")
        finally:
            events_mod.CallSession.wait_permission = original_wait
    finally:
        os.environ.pop("MAX_CONCURRENT_CALLS", None)

    # -- _PgStore lock: no interleaved executes on the shared connection -------
    store = SessionStore("")

    class SlowConn:
        def __init__(self) -> None:
            self.spans: list[tuple[float, float]] = []

        async def execute(self, sql, params=None):
            start = time.monotonic()
            await asyncio.sleep(0.05)
            self.spans.append((start, time.monotonic()))
            return "cursor"

    conn = SlowConn()
    store._db = conn
    await asyncio.gather(store._execute("a", ()), store._execute("b", ()))
    spans = sorted(conn.spans)
    check("concurrent _execute calls are serialized",
          len(spans) == 2 and spans[0][1] <= spans[1][0] + 0.001)

    # -- _PgStore lock: a stale conn reconnects exactly once --------------------
    store2 = SessionStore("")

    class FlakyConn:
        def __init__(self) -> None:
            self.fails = 1

        async def execute(self, sql, params=None):
            if self.fails:
                self.fails -= 1
                raise RuntimeError("stale")
            return "cursor"

    flaky = FlakyConn()
    store2._db = flaky
    connects: list[int] = []

    async def fake_connect() -> None:
        connects.append(1)
        store2._db = flaky

    store2.connect = fake_connect
    first, second = await asyncio.gather(
        store2._execute("a", ()), store2._execute("b", ())
    )
    check("stale conn reconnects once, both queries succeed",
          first == "cursor" and second == "cursor" and len(connects) == 1)

    print(f"\n{'ALL PASS' if not failures else f'{len(failures)} FAILURE(S): {failures}'}")
    return 0 if not failures else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))


# -- pytest adapter -----------------------------------------------------------
def test_suite() -> None:
    assert asyncio.run(main()) == 0
