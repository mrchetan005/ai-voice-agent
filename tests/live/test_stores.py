"""LIVE round-trip for ProfileStore/BookingStore (needs DATABASE_URL).

Run:  uv run --env-file .env tests/test_stores.py

Uses a test- phone prefix and deletes its own rows, so it never touches
real caller data.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import os
import sys

from whatsapp_agent.infra.stores import BookingStore, ProfileStore

PHONE = "test-919000000001"


async def main() -> int:
    failures: list[str] = []

    def check(name: str, ok: bool) -> None:
        print(f"  [{'PASS' if ok else 'FAIL'}] {name}")
        if not ok:
            failures.append(name)

    db_url = os.environ["DATABASE_URL"]
    profiles = ProfileStore(db_url)
    bookings = BookingStore(db_url)
    await profiles.connect()
    await bookings.connect()
    if profiles._db is None or bookings._db is None:
        print("  ERROR: could not connect to Postgres")
        return 1

    try:
        # -- profiles: partial upserts never blank fields -------------------
        await profiles.upsert(PHONE, name="Chetan")
        row = await profiles.load(PHONE)
        check("insert + load", row is not None and row["name"] == "Chetan")

        await profiles.upsert(PHONE, email="c@x.com")
        row = await profiles.load(PHONE)
        check("email-only upsert kept the name",
              row["name"] == "Chetan" and row["email"] == "c@x.com")

        await profiles.upsert(PHONE, name="", email=None, timezone="Asia/Kolkata")
        row = await profiles.load(PHONE)
        check("empty/None fields never blank stored values",
              row["name"] == "Chetan" and row["email"] == "c@x.com"
              and row["timezone"] == "Asia/Kolkata")

        check("unknown phone -> None", await profiles.load("test-nobody") is None)

        # -- bookings ---------------------------------------------------------
        future = dt.datetime.now(dt.UTC) + dt.timedelta(days=3)
        await bookings.add(PHONE, "test-uid-1", future, "demo call")
        rows = await bookings.list_upcoming(PHONE)
        check("add + list_upcoming",
              len(rows) == 1 and rows[0]["uid"] == "test-uid-1"
              and rows[0]["topic"] == "demo call")

        await bookings.replace_uid("test-uid-1", "test-uid-2",
                                   future + dt.timedelta(hours=2))
        rows = await bookings.list_upcoming(PHONE)
        check("replace_uid (reschedule)", rows and rows[0]["uid"] == "test-uid-2")

        await bookings.set_status("test-uid-2", "cancelled")
        check("cancelled bookings drop out of list_upcoming",
              await bookings.list_upcoming(PHONE) == [])

        past = dt.datetime.now(dt.UTC) - dt.timedelta(days=1)
        await bookings.add(PHONE, "test-uid-3", past, "old")
        check("past bookings are not 'upcoming'",
              await bookings.list_upcoming(PHONE) == [])
    finally:
        await profiles._execute(
            "DELETE FROM voiceagent_profiles WHERE phone = %s", (PHONE,))
        await bookings._execute(
            "DELETE FROM voiceagent_bookings WHERE phone = %s", (PHONE,))
        await profiles.close()
        await bookings.close()

    print(f"\n{'ALL PASS' if not failures else f'{len(failures)} FAILURE(S): {failures}'}")
    return 0 if not failures else 1


if __name__ == "__main__":
    if sys.platform == "win32":
        # psycopg async cannot run on the default ProactorEventLoop.
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    sys.exit(asyncio.run(main()))


# -- pytest adapter -----------------------------------------------------------

import pytest  # noqa: E402

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(not os.environ.get("DATABASE_URL"), reason="needs DATABASE_URL"),
]


def test_suite() -> None:
    assert asyncio.run(main()) == 0
