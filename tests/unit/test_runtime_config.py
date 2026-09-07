"""OFFLINE checks for P8: RuntimeConfig resolution precedence + validation,
GET/POST /config contracts, POST /calls (202 / idempotent replay / 409 /
422 / auth cloak). DB-free: the store degrades, Redis is fakeredis.

Run:  uv run tests/unit/test_runtime_config.py
"""

from __future__ import annotations

import asyncio
import os
import sys

import fakeredis.aioredis
import httpx

from whatsapp_agent.api.app import create_app
from whatsapp_agent.channels.call_manager import CallBusy
from whatsapp_agent.config import get_settings
from whatsapp_agent.infra.redis import RedisGateway
from whatsapp_agent.infra.runtime_config import RuntimeConfig


def _fake_gateway() -> RedisGateway:
    gw = RedisGateway(None)
    gw._redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    return gw


class FakeConfig:
    """What the /config route needs, with the real validators."""

    degraded = False

    def __init__(self) -> None:
        self.values: dict = {}

    async def set(self, key, value):
        from whatsapp_agent.infra.runtime_config import _KEYS

        if key not in _KEYS:
            raise ValueError(f"unknown config key {key!r}")
        if value is None:
            self.values.pop(key, None)
        else:
            self.values[key] = _KEYS[key][1](value)

    async def snapshot(self):
        from whatsapp_agent.infra.runtime_config import _KEYS

        settings = get_settings()
        return {
            key: {
                "value": self.values.get(key, getattr(settings, attr)),
                "source": "runtime" if key in self.values else "default",
            }
            for key, (attr, _) in _KEYS.items()
        }


class FakeCalls:
    def __init__(self) -> None:
        self.started: list[dict] = []
        self.busy = False
        self.at_capacity = False

    async def start_outbound(self, **kwargs) -> str:
        if self.at_capacity:
            raise CallBusy("at capacity")
        if self.busy:
            raise CallBusy("x")
        self.started.append(kwargs)
        return f"ref{len(self.started)}"


async def main() -> int:
    failures: list[str] = []

    def check(name: str, ok: bool) -> None:
        print(f"  [{'PASS' if ok else 'FAIL'}] {name}")
        if not ok:
            failures.append(name)

    for key in ("DEFAULT_PROVIDER", "DEFAULT_BRAIN", "ADMIN_TOKEN", "METRICS_TOKEN"):
        os.environ.pop(key, None)

    # -- RuntimeConfig resolution ------------------------------------------------
    rc = RuntimeConfig("")  # never connected: DB degraded, env-only
    check("degraded store resolves to Settings",
          rc.degraded
          and await rc.resolve("provider") == get_settings().default_provider)
    check("explicit override beats everything",
          await rc.resolve("provider", "split") == "split")

    gw = _fake_gateway()
    await gw.set_json("cfg:runtime", {"provider": "split"}, ttl_s=30)
    rc2 = RuntimeConfig("", redis=gw)
    check("runtime override (cached) beats Settings",
          await rc2.resolve("provider") == "split")
    snap = await rc2.snapshot()
    check("snapshot marks sources",
          snap["provider"] == {"value": "split", "source": "runtime"}
          and snap["brain"]["source"] == "default"
          and set(snap["provider"]) == {"value", "source"})

    for bad in (("nope", "x"), ("provider", "bogus"), ("recap_enabled", "yes"),
                ("max_concurrent_calls", "3"), ("max_concurrent_calls", True),
                ("max_concurrent_calls", -1)):
        try:
            await rc2.set(*bad)
            check(f"set{bad} rejected", False)
        except ValueError:
            check(f"set{bad} rejected", True)

    from whatsapp_agent.infra.runtime_config import _KEYS
    check("max_concurrent_calls validator accepts 5",
          _KEYS["max_concurrent_calls"][1](5) == 5)
    check("max_concurrent_calls resolves to Settings default",
          await rc.resolve("max_concurrent_calls") == 3)

    await rc2.set("provider", "gemini-live")  # DB no-op, must still bust cache
    check("set() invalidates the cfg:runtime cache",
          await rc2.resolve("provider") == get_settings().default_provider)

    # -- /config + /calls routes ----------------------------------------------------
    app = create_app()
    client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    )
    auth = {"Authorization": "Bearer adm"}

    resp = await client.get("/config")
    check("no token configured -> /config cloaked 404", resp.status_code == 404)
    resp = await client.post("/calls", json={})
    check("no token configured -> /calls cloaked 404", resp.status_code == 404)

    os.environ["ADMIN_TOKEN"] = "adm"
    try:
        resp = await client.get("/config")
        check("missing bearer -> 401", resp.status_code == 401)

        resp = await client.get("/config", headers=auth)
        check("/config 503 before lifespan wiring", resp.status_code == 503)

        app.state.config = FakeConfig()
        resp = await client.get("/config", headers=auth)
        body = resp.json()
        check("GET /config: full snapshot, all defaults",
              resp.status_code == 200 and "provider" in body
              and all(v["source"] == "default" for v in body.values()))

        resp = await client.post("/config", headers=auth,
                                 json={"provider": "split", "recap_enabled": False})
        body = resp.json()
        check("POST /config applies overrides",
              resp.status_code == 200
              and body["provider"] == {"value": "split", "source": "runtime"}
              and body["recap_enabled"]["value"] is False)

        resp = await client.post("/config", headers=auth, json={"provider": "bogus"})
        check("POST /config invalid value -> 422",
              resp.status_code == 422 and "error" in resp.json())
        resp = await client.post("/config", headers=auth, json=["not", "a", "dict"])
        check("POST /config non-object body -> 422", resp.status_code == 422)

        # -- /calls ----------------------------------------------------------------
        resp = await client.post("/calls", headers=auth, json={})
        check("/calls 503 before lifespan wiring", resp.status_code == 503)

        fake_calls = FakeCalls()
        app.state.calls = fake_calls
        app.state.redis = _fake_gateway()

        resp = await client.post("/calls", headers=auth,
                                 json={"to": "919876543210", "provider": "split"},
                                 params={})
        check("POST /calls -> 202 with call_ref",
              resp.status_code == 202 and resp.json() == {"call_ref": "ref1"}
              and fake_calls.started[0]["peer"] == "919876543210"
              and fake_calls.started[0]["provider"] == "split")

        headers = {**auth, "Idempotency-Key": "k1"}
        resp1 = await client.post("/calls", headers=headers, json={})
        resp2 = await client.post("/calls", headers=headers, json={})
        check("Idempotency-Key: first 202, replay 200 with the SAME ref",
              resp1.status_code == 202 and resp2.status_code == 200
              and resp1.json() == resp2.json()
              and len(fake_calls.started) == 2)  # replay started no new call

        fake_calls.busy = True
        resp = await client.post("/calls", headers=auth, json={})
        check("busy -> 409", resp.status_code == 409)
        fake_calls.busy = False

        resp = await client.post("/calls", headers=auth, json={"provider": "nope"})
        check("invalid provider -> 422 before anything starts",
              resp.status_code == 422 and len(fake_calls.started) == 2)
        resp = await client.post("/calls", headers=auth, json={"brain": "triple"})
        check("invalid brain -> 422", resp.status_code == 422)

        # -- capacity + brief + status ---------------------------------------------
        fake_calls.at_capacity = True
        resp = await client.post("/calls", headers=auth, json={})
        check("at capacity -> 409 with distinct error body",
              resp.status_code == 409 and resp.json() == {"error": "at capacity"})
        fake_calls.at_capacity = False

        brief = {
            "topic": "Product demo",
            "time_range": {"from": "2036-01-05T10:00:00", "to": "2036-01-06T18:00:00"},
            "attendee_name": "Chetan",
            "attendee_email": "chetan@example.com",
            "timezone": "Asia/Kolkata",
        }
        resp = await client.post("/calls", headers=auth,
                                 json={"to": "919876543210", "brief": brief})
        sent = fake_calls.started[-1]["brief"]
        check("brief -> 202 and CallBrief threaded through",
              resp.status_code == 202
              and sent is not None and sent.topic == "Product demo"
              and sent.attendee_name == "Chetan"
              and sent.attendee_email == "chetan@example.com"
              and sent.window_start is not None
              and sent.window_start.tzinfo is not None  # naive input got the brief tz
              and sent.window_start.isoformat() == "2036-01-05T10:00:00+05:30")

        started_before = len(fake_calls.started)
        for name, bad in (
            ("bad phone", {"to": "12ab", "brief": brief}),
            ("topicless brief", {"brief": {"topic": ""}}),
            ("bad email", {"brief": {"topic": "x", "attendee_email": "not-an-email"}}),
            ("from >= to", {"brief": {"topic": "x", "time_range": {
                "from": "2036-01-06T10:00:00", "to": "2036-01-05T10:00:00"}}}),
            ("range in the past", {"brief": {"topic": "x", "time_range": {
                "from": "2020-01-01T10:00:00", "to": "2020-01-02T10:00:00"}}}),
            ("bad timezone", {"brief": {"topic": "x", "timezone": "Mars/Olympus"}}),
        ):
            resp = await client.post("/calls", headers=auth, json=bad)
            check(f"{name} -> 422 with error body",
                  resp.status_code == 422 and "error" in resp.json())
        check("no call started by invalid bodies",
              len(fake_calls.started) == started_before)

        resp = await client.get("/calls/deadbeef0000", headers=auth)
        check("GET /calls/{ref} unknown -> 404", resp.status_code == 404)
        await app.state.redis.set_json(
            "call:status:abc123", {"call_ref": "abc123", "status": "in_progress",
                                   "peer": "919", "updated_at": "t"}, ttl_s=60)
        resp = await client.get("/calls/abc123", headers=auth)
        check("GET /calls/{ref} -> 200 with the status payload",
              resp.status_code == 200 and resp.json()["status"] == "in_progress")
    finally:
        del os.environ["ADMIN_TOKEN"]

    await client.aclose()
    print(f"\n{'ALL PASS' if not failures else f'{len(failures)} FAILURE(S): {failures}'}")
    return 0 if not failures else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))


# -- pytest adapter -----------------------------------------------------------
def test_suite() -> None:
    assert asyncio.run(main()) == 0
