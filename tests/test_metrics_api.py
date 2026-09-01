"""OFFLINE checks for the /report and /costs endpoints (mocked requests,
fake session store — no DB, no server).

Run:  uv run tests/test_metrics_api.py
"""

from __future__ import annotations

import asyncio
import datetime as dt
import json
import os
import sys

from aiohttp.test_utils import make_mocked_request

from appointment_booker.metrics_api import MetricsAPI

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


def request(path: str, token: str | None):
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    return make_mocked_request("GET", path, headers=headers)


async def main() -> int:
    failures: list[str] = []

    def check(name: str, ok: bool) -> None:
        print(f"  [{'PASS' if ok else 'FAIL'}] {name}")
        if not ok:
            failures.append(name)

    api = MetricsAPI()
    api._session_store = FakeSessionStore()

    os.environ.pop("METRICS_TOKEN", None)
    resp = await api.handle_costs(request("/costs", "whatever"))
    check("METRICS_TOKEN unset -> 404", resp.status == 404)

    os.environ["METRICS_TOKEN"] = "secret"
    try:
        resp = await api.handle_costs(request("/costs", None))
        check("missing bearer -> 401", resp.status == 401)
        resp = await api.handle_report(request("/report", "wrong"))
        check("wrong bearer -> 401", resp.status == 401)

        resp = await api.handle_costs(request("/costs?days=30", "secret"))
        body = json.loads(resp.body)
        check("/costs 200 with contract keys",
              resp.status == 200 and set(body) == {
                  "generated_at", "window", "currency", "sessions", "totals"})
        check("/costs sessions carry cost_breakdown",
              body["sessions"][0]["cost_breakdown"]["gemini_live"]["usd"] == 0.015)
        check("/costs totals aggregate by provider and channel",
              abs(body["totals"]["cost_usd"] - 0.021) < 1e-9
              and abs(body["totals"]["by_provider"]["whatsapp"] - 0.005) < 1e-9
              and abs(body["totals"]["by_channel"]["chat"] - 0.001) < 1e-9)

        resp = await api.handle_report(request("/report?days=7", "secret"))
        body = json.loads(resp.body)
        check("/report 200 with contract keys",
              resp.status == 200 and set(body) == {
                  "generated_at", "window", "volume", "outcomes", "conversion",
                  "latency_ms", "health", "audit", "cost"})
        check("/report volumes + outcomes",
              body["volume"]["total_sessions"] == 2
              and body["volume"]["by_channel"]["chat"] == 1
              and body["outcomes"]["booked"] == 1)
        check("/report conversion + drop-off",
              body["conversion"]["booked_rate"] == 0.5
              and body["conversion"]["drop_off_rate"] == 0.5)
        check("/report pools latency percentiles",
              body["latency_ms"]["agent_turn"]["count"] == 3
              and body["latency_ms"]["agent_turn"]["p50"] == 900.0)
        check("/report health flags + audit summary",
              body["health"]["flag_counts"]["short_session"] == 1
              and body["audit"]["audited"] == 1
              and body["audit"]["avg_score"] == 9
              and body["audit"]["latest"][0]["summary"] == "clean")

        resp = await api.handle_costs(request("/costs?days=999999", "secret"))
        check("days capped without error", resp.status == 200)
    finally:
        del os.environ["METRICS_TOKEN"]

    print(f"\n{'ALL PASS' if not failures else f'{len(failures)} FAILURE(S): {failures}'}")
    return 0 if not failures else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
