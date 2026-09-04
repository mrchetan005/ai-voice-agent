"""OFFLINE checks for audit: deterministic flags, verdict parsing, and the
sampled judge loop with a fake LLM + fake store.

Run:  uv run tests/test_audit.py
"""

from __future__ import annotations

import asyncio
import sys

from whatsapp_agent.agent.audit import _parse_verdict, deterministic_flags, run_audit

BOOKED = [{"action": "booked", "uid": "u1"}]


class FakeStore:
    def __init__(self, rows):
        self.rows = rows
        self.saved: dict[str, dict] = {}

    async def unaudited(self, since, limit):
        return self.rows[:limit]

    async def save_audit(self, session_id, verdict):
        self.saved[session_id] = verdict

    async def close(self):
        pass


class FakeLLM:
    model = "fake-judge"

    def __init__(self, reply: str) -> None:
        self.reply = reply

    async def ainvoke(self, messages):
        class _Msg:
            content = self.reply

            def __init__(inner) -> None:
                inner.usage_metadata = {"input_tokens": 100, "output_tokens": 30}

        return _Msg()


def flags(turns, actions=(), errors=(), duration=60.0, degraded=False, lang=""):
    return deterministic_flags(
        list(turns), list(actions), list(errors),
        db_degraded=degraded, duration_s=duration, language=lang,
    )


async def main() -> int:
    failures: list[str] = []

    def check(name: str, ok: bool) -> None:
        print(f"  [{'PASS' if ok else 'FAIL'}] {name}")
        if not ok:
            failures.append(name)

    # -- claimed_booking_no_api_success ------------------------------------
    claim = [("user", "book it"), ("assistant", "Great, your appointment is booked!")]
    check("false claim flagged when no API success",
          "claimed_booking_no_api_success" in flags(claim))
    check("claim NOT flagged when the booking really happened",
          "claimed_booking_no_api_success" not in flags(claim, actions=BOOKED))
    check("question form is not a claim",
          "claimed_booking_no_api_success" not in flags(
              [("user", "hi"), ("assistant", "Shall I get the appointment booked?")]))
    check("negation is not a claim",
          "claimed_booking_no_api_success" not in flags(
              [("user", "hi"), ("assistant", "Sorry, the appointment could not be booked.")]))
    check("Hindi claim flagged",
          "claimed_booking_no_api_success" in flags(
              [("user", "ok"), ("assistant", "आपका अपॉइंटमेंट बुक हो गया है।")]))

    # -- other flags -----------------------------------------------------------
    check("booking failure flagged",
          "booking_failed_during_call" in flags(claim, actions=BOOKED,
                                                errors=[{"op": "book", "error": "500"}]))
    check("email NO_REPLY flagged",
          "email_request_no_reply" in flags(claim, actions=BOOKED,
                                            errors=[{"op": "request_email", "status": "NO_REPLY"}]))
    check("db degraded flagged", "db_degraded" in flags(claim, actions=BOOKED, degraded=True))
    check("short session flagged (duration)",
          "short_session" in flags(claim, actions=BOOKED, duration=5))
    check("short session flagged (no user turns)",
          "short_session" in flags([("assistant", "hello?")], duration=60))
    check("hindi config without Devanagari output flagged",
          "language_mismatch" in flags(
              [("user", "नमस्ते"), ("assistant", "Hello, how can I help?")],
              actions=BOOKED, lang="hi-IN"))
    check("clean call has no flags", flags(claim, actions=BOOKED) == [])

    # -- verdict parsing ----------------------------------------------------------
    clean = '{"score": 8, "summary": "fine"}'
    fenced = f"```json\n{clean}\n```"
    check("clean JSON parsed", _parse_verdict(clean)["score"] == 8)
    check("fenced JSON parsed", _parse_verdict(fenced)["score"] == 8)
    garbage = _parse_verdict("I think the call was fine overall.")
    check("garbage stored as error verdict, no crash",
          garbage["error"] == "unparseable" and garbage["score"] is None)

    # -- judge loop -----------------------------------------------------------------
    rows = [
        {"session_id": "s1", "channel": "voice-inbound", "actions": BOOKED,
         "turns": [["user", "hi"], ["assistant", "booked!"]], "flags": []},
        {"session_id": "s2", "channel": "chat", "actions": [],
         "turns": [["user", "slot?"], ["assistant", "Tuesday 3pm is confirmed."]],
         "flags": ["claimed_booking_no_api_success"]},
    ]
    store = FakeStore(rows)
    llm = FakeLLM('{"score": 6, "false_booking_claim": true, "summary": "claimed unbooked slot"}')
    code = await run_audit(days=7, sample=10, llm=llm, store=store)
    check("judge audits every sampled session", code == 0 and len(store.saved) == 2)
    check("verdict + judge token usage stored",
          store.saved["s2"]["score"] == 6
          and store.saved["s2"]["judge"]["input_tokens"] == 100)

    store2 = FakeStore(rows)
    await run_audit(days=7, sample=10, llm=llm, store=store2, dry_run=True)
    check("dry run saves nothing", store2.saved == {})

    print(f"\n{'ALL PASS' if not failures else f'{len(failures)} FAILURE(S): {failures}'}")
    return 0 if not failures else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))


# -- pytest adapter -----------------------------------------------------------
def test_suite() -> None:
    assert asyncio.run(main()) == 0
