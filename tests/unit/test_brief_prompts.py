"""OFFLINE checks for the outbound-call brief: prompt block rendering,
CallBrief -> prompt text, the session-only email/profile seed, and the
book() gate accepting exactly the pre-seeded email.

Run:  uv run tests/unit/test_brief_prompts.py
"""

from __future__ import annotations

import asyncio
import datetime as dt
import sys
from zoneinfo import ZoneInfo

from whatsapp_agent.agent.prompts import (
    build_single_brain_prompt,
    render_brief_block,
)
from whatsapp_agent.capabilities.booking.service import BookingService
from whatsapp_agent.channels.call_manager import CallBrief, _apply_brief, _brief_text


class FakeCal:
    def create_booking(self, *args, **kwargs) -> dict:
        return {"uid": "u1"}


class FakeWA:
    async def send_text(self, to: str, text: str) -> dict:
        return {}


class FakeBookingStore:
    async def list_upcoming(self, phone: str) -> list:
        return []

    async def add(self, *args) -> None:
        return None


class FakeProfileStore:
    async def upsert(self, *args, **kwargs) -> None:
        return None


def _service() -> BookingService:
    return BookingService(
        FakeCal(), FakeWA(), None, FakeBookingStore(), FakeProfileStore(),
        "919876543210", 1, timezone="Asia/Kolkata", source="test",
    )


async def main() -> int:
    failures: list[str] = []

    def check(name: str, ok: bool) -> None:
        print(f"  [{'PASS' if ok else 'FAIL'}] {name}")
        if not ok:
            failures.append(name)

    # -- render_brief_block permutations ----------------------------------------
    check("no topic -> empty block", render_brief_block("") == "")
    full = render_brief_block(
        "Product demo", attendee_name="Chetan",
        window="Saturday 05 January 10:00 to 18:00 (Asia/Kolkata)",
        email="chetan@example.com",
    )
    check("full block carries topic + name + window + email read-back",
          "CALL BRIEF" in full and "Product demo" in full
          and "calling Chetan" in full
          and "Offer ONLY slots within" in full
          and "chetan@example.com" in full and "Read it back ONCE" in full)
    topic_only = render_brief_block("Quick intro call")
    check("topic-only block has no name/window/email lines",
          "Quick intro call" in topic_only
          and "You are calling" not in topic_only
          and "Offer ONLY" not in topic_only and "email" not in topic_only)

    # -- brief lands in the single-brain system prompt ---------------------------
    prompt = build_single_brain_prompt(
        business_name="Acme", timezone="Asia/Kolkata", snapshot="s",
        inbound=False, brief=full,
    )
    check("single-brain prompt includes the CALL BRIEF block",
          "CALL BRIEF" in prompt and "Product demo" in prompt)

    # -- _brief_text renders a readable window from tz-aware datetimes -----------
    tz = ZoneInfo("Asia/Kolkata")
    brief = CallBrief(
        topic="Product demo",
        window_start=dt.datetime(2036, 1, 5, 10, 0, tzinfo=tz),
        window_end=dt.datetime(2036, 1, 5, 18, 0, tzinfo=tz),
        attendee_name="Chetan",
        attendee_email="chetan@example.com",
        timezone="Asia/Kolkata",
    )
    text = _brief_text(brief)
    check("_brief_text: same-day window as 'day HH:MM to HH:MM (tz)'",
          "10:00 to 18:00" in text and "(Asia/Kolkata)" in text
          and "05 January" in text)
    check("_brief_text: no brief -> empty", _brief_text(None) == "")

    # -- _apply_brief seeds session state without clobbering ---------------------
    service = _service()
    _apply_brief(service, brief)
    check("_apply_brief seeds confirmed_email + profile",
          service.confirmed_email == "chetan@example.com"
          and service.profile == {"name": "Chetan", "email": "chetan@example.com"})

    service2 = _service()
    service2.profile = {"name": "Stored Name", "email": ""}
    _apply_brief(service2, brief)
    check("_apply_brief never clobbers a stored profile name",
          service2.profile["name"] == "Stored Name"
          and service2.profile["email"] == "chetan@example.com")

    # -- book() gate: the brief email books; any other email is still blocked ----
    result = await service.book(
        "2036-01-05T10:00:00", "Chetan", "Product demo", "chetan@example.com"
    )
    check("book() passes the email gate with the brief email",
          result.get("status") == "BOOKED" and result.get("uid") == "u1")
    await asyncio.sleep(0.05)  # let fire-and-forget notification tasks finish
    result = await service.book(
        "2036-01-05T10:00:00", "Chetan", "Product demo", "someone@else.com"
    )
    check("book() still blocks a different, unconfirmed email",
          result.get("status") == "EMAIL_NOT_CONFIRMED")

    print(f"\n{'ALL PASS' if not failures else f'{len(failures)} FAILURE(S): {failures}'}")
    return 0 if not failures else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))


# -- pytest adapter -----------------------------------------------------------
def test_suite() -> None:
    assert asyncio.run(main()) == 0
