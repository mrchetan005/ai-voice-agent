"""Cal.com API v2 client (sync httpx).

Sync on purpose: LangChain tools are plain sync callables (LangGraph runs
them in a worker thread); async callers wrap calls in asyncio.to_thread.

Self-check (live, read-only):
    uv run --env-file .env python -m appointment_booker.cal_client
"""

from __future__ import annotations

import json
import os
import sys
from datetime import date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import httpx

BASE = "https://api.cal.com/v2"
# cal-api-version is PER ENDPOINT; omitting it silently routes to an older
# controller, so every method sets its own.
V_SLOTS = "2024-09-04"
V_BOOKINGS = "2026-02-25"
V_EVENT_TYPES = "2024-06-14"


class CalClient:
    def __init__(self, api_key: str | None = None, timeout: float = 20.0) -> None:
        self._key = api_key or os.environ.get("CAL_API_KEY") or ""
        if not self._key:
            raise RuntimeError("CAL_API_KEY missing")
        self._http = httpx.Client(base_url=BASE, timeout=timeout)

    def _headers(self, version: str) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._key}",
            "cal-api-version": version,
            "Content-Type": "application/json",
        }

    def me(self) -> dict[str, Any]:
        resp = self._http.get("/me", headers=self._headers(V_EVENT_TYPES))
        resp.raise_for_status()
        return resp.json().get("data", {})

    def list_event_types(self, username: str | None = None) -> list[dict[str, Any]]:
        params: dict[str, str] = {}
        if username:
            params["username"] = username
        resp = self._http.get(
            "/event-types", params=params, headers=self._headers(V_EVENT_TYPES)
        )
        resp.raise_for_status()
        data = resp.json().get("data", [])
        # Some controller versions nest under eventTypeGroups; flatten defensively.
        if isinstance(data, dict):
            data = [
                et
                for group in data.get("eventTypeGroups", [])
                for et in group.get("eventTypes", [])
            ]
        return data

    def get_slots(
        self,
        event_type_id: int,
        start: date,
        end: date,
        timezone: str = "Asia/Kolkata",
    ) -> dict[str, list[dict[str, str]]]:
        """Returns {'YYYY-MM-DD': [{'start': iso, ...}, ...]}."""
        resp = self._http.get(
            "/slots",
            params={
                "eventTypeId": str(event_type_id),
                "start": start.isoformat(),
                "end": end.isoformat(),
                "timeZone": timezone,
            },
            headers=self._headers(V_SLOTS),
        )
        resp.raise_for_status()
        return resp.json().get("data", {})

    def create_booking(
        self,
        event_type_id: int,
        start_utc_iso: str,
        name: str,
        timezone: str = "Asia/Kolkata",
        email: str | None = None,
        phone: str | None = None,
        metadata: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        attendee: dict[str, Any] = {"name": name, "timeZone": timezone, "language": "en"}
        if email:
            attendee["email"] = email
        if phone:
            attendee["phoneNumber"] = phone
        resp = self._http.post(
            "/bookings",
            json={
                "eventTypeId": event_type_id,
                "start": start_utc_iso,  # must be UTC ISO 8601
                "attendee": attendee,
                "metadata": metadata or {"source": "voiceagent-appointment-booker"},
            },
            headers=self._headers(V_BOOKINGS),
        )
        resp.raise_for_status()
        return resp.json().get("data", {})

    def cancel_booking(self, uid: str, reason: str = "cancelled by agent") -> dict[str, Any]:
        resp = self._http.post(
            f"/bookings/{uid}/cancel",
            json={"cancellationReason": reason},
            headers=self._headers(V_BOOKINGS),
        )
        resp.raise_for_status()
        return resp.json().get("data", {})

    def close(self) -> None:
        self._http.close()


def _self_check() -> int:
    cal = CalClient()
    me = cal.me()
    username = me.get("username") or ""
    print(f"[ok] authenticated as {username or me.get('email', '?')}")

    event_types = cal.list_event_types(username or None)
    if not event_types:
        print("[!!] no event types found — create one in Cal.com first")
        return 1
    for et in event_types[:5]:
        print(
            f"  event type {et.get('id')}: {et.get('slug')} "
            f"({et.get('lengthInMinutes') or et.get('length')} min)"
        )

    et_id = int(os.environ.get("CAL_EVENT_TYPE_ID") or event_types[0]["id"])
    org_tz = ZoneInfo(os.environ.get("CAL_TIMEZONE", "Asia/Kolkata"))
    tomorrow = datetime.now(org_tz).date() + timedelta(days=1)
    slots = cal.get_slots(et_id, tomorrow, tomorrow + timedelta(days=2))
    total = sum(len(v) for v in slots.values())
    print(f"[ok] event type {et_id}: {total} slots in next 3 days")
    print(json.dumps({k: v[:2] for k, v in list(slots.items())[:2]}, indent=2))
    cal.close()
    return 0


if __name__ == "__main__":
    sys.exit(_self_check())
