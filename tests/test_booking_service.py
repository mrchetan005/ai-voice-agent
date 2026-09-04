"""OFFLINE checks for BookingService (fakes for Cal/WhatsApp/stores).

Run:  uv run tests/test_booking_service.py

Covers the email gates (REQUIRED + confirmed), the WhatsApp button confirm
flow (confirm / edit / stale tap), the duplicate-booking guard, and the
cancel/reschedule paths — the rules that make bookings trustworthy.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import sys

from whatsapp_agent.capabilities.booking.service import BookingService
from whatsapp_agent.channels.events import ButtonReply, WebhookHub


class FakeCal:
    def __init__(self) -> None:
        self.created: list[tuple] = []
        self.cancelled: list[tuple] = []
        self.rescheduled: list[tuple] = []

    def create_booking(self, *args, **kwargs):
        self.created.append(args)
        return {"uid": "uid-1"}

    def cancel_booking(self, uid, reason):
        self.cancelled.append((uid, reason))
        return {}

    def reschedule_booking(self, uid, start_utc_iso, reason="rescheduled by agent"):
        self.rescheduled.append((uid, start_utc_iso))
        return {"uid": "uid-2", "rescheduledFromUid": uid}


class FakeWA:
    def __init__(self) -> None:
        self.texts: list[tuple[str, str]] = []
        self.buttons: list[tuple[str, str, list]] = []

    async def send_text(self, to, body):
        self.texts.append((to, body))

    async def send_button_message(self, to, body, buttons):
        self.buttons.append((to, body, buttons))


class FakeBookingStore:
    def __init__(self) -> None:
        self.upcoming: list[dict] = []
        self.added: list[tuple] = []
        self.statuses: list[tuple] = []
        self.replaced: list[tuple] = []

    async def list_upcoming(self, phone):
        return list(self.upcoming)

    async def add(self, phone, uid, start_utc, topic):
        self.added.append((phone, uid, start_utc, topic))

    async def set_status(self, uid, status):
        self.statuses.append((uid, status))

    async def replace_uid(self, old_uid, new_uid, new_start_utc):
        self.replaced.append((old_uid, new_uid, new_start_utc))

    async def close(self):
        pass


class FakeProfileStore:
    def __init__(self) -> None:
        self.upserts: list[dict] = []

    async def load(self, phone):
        return None

    async def upsert(self, phone, name=None, email=None, timezone=None):
        self.upserts.append({"phone": phone, "name": name, "email": email,
                             "timezone": timezone})

    async def close(self):
        pass


async def main() -> int:
    failures: list[str] = []

    def check(name: str, ok: bool) -> None:
        print(f"  [{'PASS' if ok else 'FAIL'}] {name}")
        if not ok:
            failures.append(name)

    cal, wa, hub = FakeCal(), FakeWA(), WebhookHub(verify_token="x")
    bookings, profiles = FakeBookingStore(), FakeProfileStore()
    service = BookingService(
        cal, wa, hub, bookings, profiles,
        recipient="919999999999", event_type_id=1,
        timezone="Asia/Kolkata", source="test",
    )

    # -- email gates -------------------------------------------------------
    result = await service.book("2026-09-10T15:00:00", "Chetan", "demo", "")
    check("empty email -> EMAIL_REQUIRED", result["status"] == "EMAIL_REQUIRED")

    result = await service.book("2026-09-10T15:00:00", "Chetan", "demo", "not-an-email")
    check("garbage email -> EMAIL_REQUIRED", result["status"] == "EMAIL_REQUIRED")

    result = await service.book("2026-09-10T15:00:00", "Chetan", "demo", "c@x.com")
    check("valid but unconfirmed -> EMAIL_NOT_CONFIRMED",
          result["status"] == "EMAIL_NOT_CONFIRMED")
    check("no Cal call slipped through the gates", not cal.created)

    # -- confirmation flow ---------------------------------------------------
    result = await service.send_email_confirmation("bad email")
    check("invalid email -> INVALID_EMAIL", result["status"] == "INVALID_EMAIL")

    result = await service.send_email_confirmation("c@x.com")
    check("buttons sent with email_ok id",
          result["status"] == "CONFIRMATION_SENT"
          and wa.buttons and wa.buttons[-1][2][0][0] == "email_ok:c@x.com")

    hub.button_replies.put_nowait(ButtonReply(
        from_number="919999999999", button_id="email_ok:stale@old.com", title="Confirm"))
    hub.button_replies.put_nowait(ButtonReply(
        from_number="919999999999", button_id="email_ok:c@x.com", title="Confirm"))
    result = await service.wait_email_confirmation(timeout_s=1.0)
    check("stale tap skipped, matching tap -> CONFIRMED",
          result["status"] == "CONFIRMED" and service.confirmed_email == "c@x.com")
    await asyncio.sleep(0.05)  # let fire-and-forget upsert land
    check("confirmed email upserted to profile",
          any(u["email"] == "c@x.com" for u in profiles.upserts))

    # -- edit path -----------------------------------------------------------
    await service.send_email_confirmation("d@y.com")
    hub.button_replies.put_nowait(ButtonReply(
        from_number="919999999999", button_id="email_edit", title="Edit"))
    result = await service.wait_email_confirmation(timeout_s=1.0)
    check("edit tap -> EDIT_REQUESTED + retype text sent",
          result["status"] == "EDIT_REQUESTED"
          and any("correct email" in body for _, body in wa.texts))
    check("edit did not confirm the pending email",
          service.confirmed_email == "c@x.com")

    result = await service.wait_email_confirmation(timeout_s=0.2)
    check("silence -> NO_REPLY (re-callable)", result["status"] == "NO_REPLY")

    # -- booking + duplicate guard ---------------------------------------------
    result = await service.book("2026-09-10T15:00:00", "Chetan", "demo", "c@x.com")
    check("confirmed email books -> BOOKED uid-1",
          result["status"] == "BOOKED" and result["uid"] == "uid-1")
    await asyncio.sleep(0.05)
    check("local booking row recorded", bookings.added
          and bookings.added[0][1] == "uid-1")
    check("WA confirmation fired",
          any("Appointment Confirmed" in body for _, body in wa.texts))
    check("session action logged for the call recap",
          service.session_actions and service.session_actions[0]["action"] == "booked")

    bookings.upcoming = [{
        "uid": "uid-1",
        "start_utc": dt.datetime(2026, 9, 10, 9, 30, tzinfo=dt.UTC),
        "topic": "demo",
    }]
    result = await service.book("2026-09-12T15:00:00", "Chetan", "demo2", "c@x.com")
    check("existing booking -> EXISTING_BOOKING with details",
          result["status"] == "EXISTING_BOOKING"
          and result["existing"][0]["uid"] == "uid-1"
          and "when_local" in result["existing"][0])

    result = await service.book(
        "2026-09-12T15:00:00", "Chetan", "demo2", "c@x.com", book_anyway=True
    )
    check("book_anyway bypasses the duplicate guard", result["status"] == "BOOKED")

    # -- cancel / reschedule ----------------------------------------------------
    result = await service.cancel("uid-1", "changed plans")
    await asyncio.sleep(0.05)
    check("cancel -> CANCELLED + Cal called + status row",
          result["status"] == "CANCELLED"
          and cal.cancelled == [("uid-1", "changed plans")]
          and ("uid-1", "cancelled") in bookings.statuses)

    result = await service.reschedule("uid-2", "2026-09-15T11:00:00")
    await asyncio.sleep(0.05)
    check("reschedule -> RESCHEDULED with the NEW uid",
          result["status"] == "RESCHEDULED" and result["uid"] == "uid-2")
    check("reschedule sent UTC to Cal and replaced the uid locally",
          cal.rescheduled and cal.rescheduled[0][1].endswith("Z")
          and bookings.replaced)
    check("WA reschedule notice fired",
          any("Rescheduled" in body for _, body in wa.texts))

    listing = await service.list_bookings()
    check("list_bookings formats rows for the model",
          listing["bookings"][0]["uid"] == "uid-1"
          and "when_local" in listing["bookings"][0])

    print(f"\n{'ALL PASS' if not failures else f'{len(failures)} FAILURE(S): {failures}'}")
    return 0 if not failures else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
