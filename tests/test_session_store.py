"""LIVE round-trip for SessionStore (needs DATABASE_URL).

Run:  uv run --env-file .env tests/test_session_store.py

Uses test- prefixed session ids and deletes its own rows.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import os
import sys

from whatsapp_agent.infra.stores import SessionStore

SID = "test-session-0001"


async def main() -> int:
    failures: list[str] = []

    def check(name: str, ok: bool) -> None:
        print(f"  [{'PASS' if ok else 'FAIL'}] {name}")
        if not ok:
            failures.append(name)

    store = SessionStore(os.environ["DATABASE_URL"])
    await store.connect()
    if store.degraded:
        print("  ERROR: could not connect to Postgres")
        return 1

    now = dt.datetime.now(dt.UTC)
    row = {
        "session_id": SID, "phone": "test-919", "channel": "voice-inbound",
        "brain": "single", "started_at": now - dt.timedelta(minutes=3),
        "ended_at": now, "duration_s": 180.0, "user_turns": 5,
        "assistant_turns": 5, "outcome": "booked",
        "actions": [{"action": "booked", "uid": "test-u1"}],
        "turns": [["user", "hi"], ["assistant", "hello!"]],
        "latency": {"raw": {"agent_turn_ms": [900.0]}},
        "usage": {"gemini_live": {"total_tokens": 1234}},
        "cost_usd": 0.0123,
        "cost_breakdown": {"gemini_live": {"usd": 0.0123, "units": {}}},
        "flags": ["short_session"],
    }
    try:
        await store.record(row)
        await store.record(row)  # duplicate -> ON CONFLICT DO NOTHING

        rows = await store.sessions_since(now - dt.timedelta(hours=1))
        mine = [r for r in rows if r["session_id"] == SID]
        check("record + read back (deduped)", len(mine) == 1)
        got = mine[0]
        check("jsonb fields round-trip",
              got["actions"][0]["uid"] == "test-u1"
              and got["usage"]["gemini_live"]["total_tokens"] == 1234
              and got["flags"] == ["short_session"])
        check("numeric cost -> float", abs(got["cost_usd"] - 0.0123) < 1e-9)

        pending = await store.unaudited(now - dt.timedelta(hours=1), 50)
        check("unaudited returns the row with its transcript",
              any(r["session_id"] == SID and r["turns"] for r in pending))

        await store.save_audit(SID, {"score": 7, "summary": "ok"})
        rows = await store.sessions_since(now - dt.timedelta(hours=1))
        got = next(r for r in rows if r["session_id"] == SID)
        check("audit verdict saved", got["audit"]["score"] == 7
              and got["audited_at"] is not None)
        pending = await store.unaudited(now - dt.timedelta(hours=1), 50)
        check("audited row leaves the unaudited pool",
              not any(r["session_id"] == SID for r in pending))
    finally:
        await store._execute(
            "DELETE FROM voiceagent_sessions WHERE session_id = %s", (SID,))
        await store.close()

    print(f"\n{'ALL PASS' if not failures else f'{len(failures)} FAILURE(S): {failures}'}")
    return 0 if not failures else 1


if __name__ == "__main__":
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    sys.exit(asyncio.run(main()))
