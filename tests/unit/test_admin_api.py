"""OFFLINE checks for the FastAPI surface: /report + /costs contracts
(unchanged from the aiohttp era), webhook verify + HMAC signature, /health.

Run:  uv run tests/unit/test_admin_api.py
"""

from __future__ import annotations

import asyncio
import datetime as dt
import hashlib
import hmac
import json
import os
import sys

import httpx

from whatsapp_agent.api.app import create_app
from whatsapp_agent.api.routes import admin

NOW = dt.datetime.now(dt.UTC)

ROWS = [
    {
        "session_id": "s1", "phone": "919", "channel": "voice-inbound",
        "brain": "single", "started_at": NOW - dt.timedelta(hours=2),
        "ended_at": NOW, "duration_s": 180.0, "user_turns": 6,
        "assistant_turns": 6, "outcome": "booked",
        "actions": [{"action": "booked", "uid": "u1"}],
        "latency": {"raw": {"agent_turn_ms": [900.0, 1100.0, 800.0]}},
        "usage": {}, "cost_usd": 0.02,
        "cost_breakdown": {"gemini_live": {"usd": 0.015, "units": {}},
                           "whatsapp": {"usd": 0.005, "units": {"messages": 2}}},
        "flags": [], "audit": {"score": 9, "summary": "clean"}, "audited_at": NOW,
    },
    {
        "session_id": "s2", "phone": "918", "channel": "chat", "brain": "",
        "started_at": NOW - dt.timedelta(hours=1), "ended_at": NOW,
        "duration_s": 5.0, "user_turns": 1, "assistant_turns": 1,
        "outcome": "no_action", "actions": [], "latency": {},
        "usage": {}, "cost_usd": 0.001,
        "cost_breakdown": {"gemini_flash": {"usd": 0.001, "units": {}}},
        "flags": ["short_session"], "audit": None, "audited_at": None,
    },
]


class FakeSessionStore:
    degraded = False

    async def sessions_since(self, since):
        return ROWS


async def main() -> int:
    failures: list[str] = []

    def check(name: str, ok: bool) -> None:
        print(f"  [{'PASS' if ok else 'FAIL'}] {name}")
        if not ok:
            failures.append(name)

    app = create_app()
    admin._metrics._session_store = FakeSessionStore()
    client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    )
    auth = {"Authorization": "Bearer secret"}

    # -- admin contracts (must match the pre-FastAPI JSON exactly) ------------
    os.environ.pop("METRICS_TOKEN", None)
    resp = await client.get("/costs", headers=auth)
    check("METRICS_TOKEN unset -> 404", resp.status_code == 404
          and resp.json() == {"error": "not found"})

    os.environ["METRICS_TOKEN"] = "secret"
    try:
        resp = await client.get("/costs")
        check("missing bearer -> 401", resp.status_code == 401)
        resp = await client.get("/report", headers={"Authorization": "Bearer no"})
        check("wrong bearer -> 401", resp.status_code == 401)

        resp = await client.get("/costs?days=30", headers=auth)
        body = resp.json()
        check("/costs 200 with contract keys",
              resp.status_code == 200 and set(body) == {
                  "generated_at", "window", "currency", "sessions", "totals"})
        check("/costs totals aggregate by provider and channel",
              abs(body["totals"]["cost_usd"] - 0.021) < 1e-9
              and abs(body["totals"]["by_provider"]["whatsapp"] - 0.005) < 1e-9
              and abs(body["totals"]["by_channel"]["chat"] - 0.001) < 1e-9)

        resp = await client.get("/report?days=7", headers=auth)
        body = resp.json()
        check("/report 200 with contract keys",
              resp.status_code == 200 and set(body) == {
                  "generated_at", "window", "volume", "outcomes", "conversion",
                  "latency_ms", "health", "audit", "cost"})
        check("/report pools latency + audit summary",
              body["latency_ms"]["agent_turn"]["count"] == 3
              and body["audit"]["avg_score"] == 9
              and body["conversion"]["drop_off_rate"] == 0.5)

        resp = await client.get("/costs?days=999999", headers=auth)
        check("days capped without error", resp.status_code == 200)
    finally:
        del os.environ["METRICS_TOKEN"]

    # -- webhook verify handshake ------------------------------------------------
    resp = await client.get("/webhook", params={
        "hub.mode": "subscribe", "hub.verify_token": "voiceagent",
        "hub.challenge": "12345",
    })
    check("verify handshake echoes challenge",
          resp.status_code == 200 and resp.text == "12345")
    resp = await client.get("/webhook", params={
        "hub.mode": "subscribe", "hub.verify_token": "WRONG", "hub.challenge": "x",
    })
    check("verify with wrong token -> 403", resp.status_code == 403)

    # -- webhook signature -----------------------------------------------------------
    payload = {"entry": [{"changes": [{"field": "messages", "value": {"messages": [
        {"type": "text", "from": "919", "text": {"body": "hello"}}]}}]}]}
    raw = json.dumps(payload).encode()

    os.environ.pop("WHATSAPP_APP_SECRET", None)
    resp = await client.post("/webhook", content=raw,
                             headers={"content-type": "application/json"})
    check("secret unset (dev): accepted and routed",
          resp.status_code == 200
          and app.state.router.chat_texts.get_nowait().text == "hello")

    os.environ["WHATSAPP_APP_SECRET"] = "topsecret"
    try:
        resp = await client.post("/webhook", content=raw, headers={
            "content-type": "application/json",
            "X-Hub-Signature-256": "sha256=deadbeef",
        })
        check("bad signature -> 403 and NOT routed",
              resp.status_code == 403 and app.state.router.chat_texts.empty())

        good = "sha256=" + hmac.new(b"topsecret", raw, hashlib.sha256).hexdigest()
        resp = await client.post("/webhook", content=raw, headers={
            "content-type": "application/json", "X-Hub-Signature-256": good,
        })
        check("valid signature -> 200 and routed",
              resp.status_code == 200
              and app.state.router.chat_texts.get_nowait().text == "hello")
    finally:
        del os.environ["WHATSAPP_APP_SECRET"]

    # -- health --------------------------------------------------------------------
    resp = await client.get("/health")
    body = resp.json()
    check("/health 200 with expected keys",
          resp.status_code == 200 and set(body) == {
              "status", "redis", "db", "active_call", "active_calls", "version"}
          and body["redis"] == "disabled")

    await client.aclose()
    print(f"\n{'ALL PASS' if not failures else f'{len(failures)} FAILURE(S): {failures}'}")
    return 0 if not failures else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))


# -- pytest adapter -----------------------------------------------------------
def test_suite() -> None:
    assert asyncio.run(main()) == 0
