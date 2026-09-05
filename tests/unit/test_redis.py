"""OFFLINE checks for the Redis layer: RedisGateway fail-open semantics,
webhook gate (dedup + per-phone rate limit), slots cache-aside and the
ProfileStore cache. Runs on fakeredis — no server needed.

Run:  uv run tests/unit/test_redis.py
"""

from __future__ import annotations

import asyncio
import datetime as dt
import sys

import fakeredis.aioredis

from whatsapp_agent.api.routes.webhooks import make_webhook_gate
from whatsapp_agent.capabilities.booking import cal_client
from whatsapp_agent.infra.redis import RedisGateway
from whatsapp_agent.infra.stores import ProfileStore


def _fake_gateway() -> RedisGateway:
    gw = RedisGateway(None)
    gw._redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    return gw


class _BrokenClient:
    """Every awaited call raises — simulates Redis down mid-flight."""

    def __getattr__(self, name):
        async def fail(*args, **kwargs):
            raise ConnectionError("redis down")

        return fail

    def pipeline(self):
        raise ConnectionError("redis down")


def _message(mid: str, phone: str, text: str = "hi") -> dict:
    return {"type": "text", "id": mid, "from": phone, "text": {"body": text}}


def _payload(*, messages: list | None = None, calls: list | None = None) -> dict:
    value: dict = {}
    if messages is not None:
        value["messages"] = messages
    if calls is not None:
        value["calls"] = calls
    return {"entry": [{"changes": [{"field": "x", "value": value}]}]}


async def main() -> int:
    failures: list[str] = []

    def check(name: str, ok: bool) -> None:
        print(f"  [{'PASS' if ok else 'FAIL'}] {name}")
        if not ok:
            failures.append(name)

    # -- disabled gateway (REDIS_URL unset): permissive everywhere -------------
    off = RedisGateway(None)
    check("disabled: not enabled and everything fail-open",
          not off.enabled
          and not await off.ping()
          and not await off.dedup_seen("k")
          and not await off.rate_limited("k", 1)
          and await off.get_json("k") is None
          and await off.acquire_lock("k", "t", 60))
    await off.set_json("k", {}, 60)  # must not raise
    await off.delete("k")
    await off.release_lock("k", "t")
    await off.aclose()

    # -- broken client: same permissive answers, no exceptions -----------------
    broken = RedisGateway(None)
    broken._redis = _BrokenClient()
    check("redis down: every op fails open",
          not await broken.ping()
          and not await broken.dedup_seen("k")
          and not await broken.rate_limited("k", 1)
          and await broken.get_json("k") is None
          and await broken.acquire_lock("k", "t", 60))
    await broken.set_json("k", {"a": 1}, 60)
    await broken.release_lock("k", "t")

    # -- live semantics on fakeredis ----------------------------------------------
    gw = _fake_gateway()
    check("ping ok", await gw.ping() and gw.enabled)
    check("dedup: first sighting passes, second is a duplicate",
          not await gw.dedup_seen("wa:dedup:m1") and await gw.dedup_seen("wa:dedup:m1"))

    limited = [await gw.rate_limited("wa:rl:phone:919", 3) for _ in range(4)]
    check("rate limit 3/min: 3 pass, 4th limited", limited == [False, False, False, True])

    await gw.set_json("j", {"a": [1, 2]}, ttl_s=60)
    check("json roundtrip", await gw.get_json("j") == {"a": [1, 2]})
    await gw.delete("j")
    check("delete removes the key", await gw.get_json("j") is None)

    check("lock: granted once, contended second",
          await gw.acquire_lock("call:active:919", "tok1", 60)
          and not await gw.acquire_lock("call:active:919", "tok2", 60))
    await gw.release_lock("call:active:919", "WRONG")
    check("release with wrong token keeps the lock",
          not await gw.acquire_lock("call:active:919", "tok3", 60))
    await gw.release_lock("call:active:919", "tok1")
    check("release with right token frees it",
          await gw.acquire_lock("call:active:919", "tok3", 60))

    # -- webhook gate ---------------------------------------------------------------
    gate = make_webhook_gate(_fake_gateway(), rate_per_min=2)

    p1 = await gate(_payload(messages=[_message("mA", "111")]))
    p2 = await gate(_payload(messages=[_message("mA", "111")]))  # Meta retry
    check("gate: duplicate message dropped, original kept",
          len(p1["entry"][0]["changes"][0]["value"]["messages"]) == 1
          and p2["entry"][0]["changes"][0]["value"]["messages"] == [])

    p3 = await gate(_payload(messages=[_message("mB", "111")]))
    p4 = await gate(_payload(messages=[_message("mC", "111")]))
    check("gate: 3rd message in the window rate-limited (2/min)",
          len(p3["entry"][0]["changes"][0]["value"]["messages"]) == 1
          and p4["entry"][0]["changes"][0]["value"]["messages"] == [])

    # Call events share one call id across connect/terminate — the event
    # name is part of the dedup key, so terminate must NOT be dropped.
    gate2 = make_webhook_gate(_fake_gateway(), rate_per_min=100)
    c1 = await gate2(_payload(calls=[{"id": "wacid1", "event": "connect", "from": "111"}]))
    c2 = await gate2(_payload(calls=[{"id": "wacid1", "event": "terminate", "from": "111"}]))
    c3 = await gate2(_payload(calls=[{"id": "wacid1", "event": "terminate", "from": "111"}]))
    check("gate: terminate passes after connect; retried terminate dropped",
          len(c1["entry"][0]["changes"][0]["value"]["calls"]) == 1
          and len(c2["entry"][0]["changes"][0]["value"]["calls"]) == 1
          and c3["entry"][0]["changes"][0]["value"]["calls"] == [])

    # -- slots cache-aside ---------------------------------------------------------
    calls_made = []

    class _FakeCal:
        def get_slots(self, event_type_id, start, end, timezone):
            calls_made.append(start)
            return {"2026-09-07": [{"start": "2026-09-07T10:00:00"}]}

    cal_client._slots_memo.clear()
    gw2 = _fake_gateway()
    day = dt.date(2026, 9, 7)
    s1 = await cal_client.get_slots_cached(_FakeCal(), 1, day, day, "Asia/Kolkata", redis=gw2)
    s2 = await cal_client.get_slots_cached(_FakeCal(), 1, day, day, "Asia/Kolkata", redis=gw2)
    check("slots: in-process memo — one Cal call for two reads",
          len(calls_made) == 1 and s1 == s2)
    cal_client._slots_memo.clear()  # simulate a restart: Redis layer serves
    s3 = await cal_client.get_slots_cached(_FakeCal(), 1, day, day, "Asia/Kolkata", redis=gw2)
    check("slots: Redis serves after memo loss, still one Cal call",
          len(calls_made) == 1 and s3 == s1)
    cal_client._slots_memo.clear()

    # -- profile cache ---------------------------------------------------------------
    gw3 = _fake_gateway()
    store = ProfileStore("", redis=gw3)  # no connect(): DB degraded on purpose
    await gw3.set_json(
        "profile:919", {"name": "Priya", "email": "p@x.com", "timezone": ""}, ttl_s=60
    )
    check("profile: cache hit without touching the DB",
          (await store.load("919")) == {"name": "Priya", "email": "p@x.com", "timezone": ""})
    await store.upsert("919", email="new@x.com")  # DB no-op, must still invalidate
    check("profile: upsert invalidates the cache",
          await store.load("919") is None)

    print(f"\n{'ALL PASS' if not failures else f'{len(failures)} FAILURE(S): {failures}'}")
    return 0 if not failures else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))


# -- pytest adapter -----------------------------------------------------------
def test_suite() -> None:
    assert asyncio.run(main()) == 0
