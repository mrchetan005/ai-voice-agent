"""Shared booking operations for BOTH brain modes and chat.

One BookingService per session/caller. Native (single-brain) tools await
these methods directly; LangGraph tools bridge via _run_on_loop. Keeping
the bodies here is what prevents the two tool sets from drifting — the old
duplicated book_appointment produced two separate placeholder-email bugs.

Email policy: booking REQUIRES an email that the caller has confirmed via
WhatsApp reply buttons (or a stored profile email). There is no placeholder
fallback — a fake address means Cal.com mails the invite to nobody.
"""

from __future__ import annotations

import asyncio
import contextlib
import datetime as dt
import logging
import re
import time
from collections.abc import Awaitable, Callable
from typing import Any
from zoneinfo import ZoneInfo

from whatsapp_agent.capabilities.booking.cal_client import CalClient
from whatsapp_agent.channels.client import WhatsAppClient
from whatsapp_agent.channels.events import WebhookHub
from whatsapp_agent.infra.stores import BookingStore, ProfileStore

logger = logging.getLogger("whatsapp_agent")

EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+\.[\w.]+")

EMAIL_REQUEST_TEXT = (
    "📧 To finish your booking, please reply here with your email address."
)
EMAIL_EDIT_TEXT = "No problem — please type the correct email address here."


def format_confirmation(
    when_local: dt.datetime, timezone: str, topic: str, uid: str
) -> str:
    """Human-readable WhatsApp confirmation (WA markdown: *bold*, _italic_).

    Raw ISO timestamps and dangling empty fields read like debug output —
    spell the datetime out and skip what's missing.
    """
    when = when_local.strftime("%A, %d %B %Y at %I:%M %p")
    lines = ["✅ *Appointment Confirmed*", "", f"📅 {when}", f"🌏 {timezone}"]
    if topic.strip():
        lines.append(f"📝 {topic.strip()}")
    lines += ["", f"Ref: {uid}", "_Reply here if you need to reschedule._"]
    return "\n".join(lines)


def format_reschedule(when_local: dt.datetime, timezone: str, uid: str) -> str:
    when = when_local.strftime("%A, %d %B %Y at %I:%M %p")
    return "\n".join([
        "🔁 *Appointment Rescheduled*", "", f"📅 {when}", f"🌏 {timezone}",
        "", f"Ref: {uid}", "_Reply here if you need anything else._",
    ])


def format_cancellation(uid: str) -> str:
    return "\n".join([
        "❌ *Appointment Cancelled*", "", f"Ref: {uid}",
        "_Reply here whenever you'd like to book again._",
    ])


class BookingService:
    def __init__(
        self,
        cal: CalClient,
        wa: WhatsAppClient,
        hub: WebhookHub,
        booking_store: BookingStore,
        profile_store: ProfileStore,
        recipient: str,
        event_type_id: int,
        timezone: str = "Asia/Kolkata",
        source: str = "voice-agent",
    ) -> None:
        self.cal = cal
        self.wa = wa
        self.hub = hub
        self.booking_store = booking_store
        self.profile_store = profile_store
        self.recipient = recipient
        self.event_type_id = event_type_id
        self.timezone = timezone
        self.source = source
        self._tz = ZoneInfo(timezone)
        # The hard booking gate checks THIS, not the model's email argument:
        # set only by a WhatsApp Confirm tap or a stored profile email.
        self.confirmed_email: str | None = None
        self._pending_email: str | None = None
        self.profile: dict[str, str] | None = None
        # Ground truth for the post-call recap (uids/times from API results,
        # never from what the model believes it did).
        self.session_actions: list[dict[str, str]] = []
        # Failures + notable non-successes this session (feeds audit flags).
        self.session_errors: list[dict[str, str]] = []
        # Optional UsageMeter (set by main.py) — WhatsApp message counting.
        self.meter: Any = None
        # How mid-call email replies arrive; single-brain overrides this
        # with TextBridge.wait_email (the bridge owns the text queue there).
        self.email_waiter: Callable[[float], Awaitable[str | None]] = self._hub_email_waiter

    # -- helpers ----------------------------------------------------------

    def _count_wa(self, messages: int = 1) -> None:
        if self.meter is not None:
            self.meter.add("whatsapp", "messages", messages)

    def _bg(self, coro: Awaitable[Any]) -> None:
        """Fire-and-forget on the running loop — notifications and store
        writes must never sit between the model and its next spoken word."""
        task = asyncio.get_running_loop().create_task(self._swallow(coro))
        del task  # lifetime managed by the loop

    @staticmethod
    async def _swallow(coro: Awaitable[Any]) -> None:
        with contextlib.suppress(Exception):
            await coro

    async def _hub_email_waiter(self, wait_s: float) -> str | None:
        stop_at = time.monotonic() + wait_s
        while (remaining := stop_at - time.monotonic()) > 0:
            try:
                msg = await self.hub.wait_text(timeout_s=remaining)
            except TimeoutError:
                return None
            if match := EMAIL_RE.search(msg.text):
                return match.group()
        return None

    def _localize(self, start_local_iso: str) -> dt.datetime:
        parsed = dt.datetime.fromisoformat(start_local_iso)
        if parsed.tzinfo is None:
            # Snapshot times are org-local; never trust the OS timezone.
            parsed = parsed.replace(tzinfo=self._tz)
        return parsed

    @staticmethod
    def _utc_iso(when: dt.datetime) -> str:
        return when.astimezone(dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")

    # -- email collection + confirmation -----------------------------------

    async def request_email(self, wait_s: float) -> dict[str, Any]:
        await self.wa.send_text(self.recipient, EMAIL_REQUEST_TEXT)
        self._count_wa()
        email = await self.email_waiter(wait_s)
        if email:
            return {"email": email}
        self.session_errors.append({"op": "request_email", "status": "NO_REPLY"})
        return {"status": "NO_REPLY"}

    async def send_email_confirmation(self, email: str) -> dict[str, Any]:
        email = email.strip()
        if not EMAIL_RE.fullmatch(email):
            return {"status": "INVALID_EMAIL"}
        self._pending_email = email
        await self.wa.send_button_message(
            self.recipient,
            f"📧 Should I use this email for your booking?\n\n*{email}*",
            [(f"email_ok:{email}", "Confirm"), ("email_edit", "Edit")],
        )
        self._count_wa()
        return {"status": "CONFIRMATION_SENT"}

    async def wait_email_confirmation(self, timeout_s: float = 40.0) -> dict[str, Any]:
        """Short and re-callable on purpose: dual-brain turns are killed at
        60 s, so the model re-calls on NO_REPLY instead of blocking."""
        deadline = time.monotonic() + timeout_s
        while (remaining := deadline - time.monotonic()) > 0:
            try:
                reply = await self.hub.wait_button(timeout_s=remaining)
            except TimeoutError:
                break
            if reply.button_id == f"email_ok:{self._pending_email}":
                self.confirmed_email = self._pending_email
                self._bg(self.profile_store.upsert(self.recipient, email=self.confirmed_email))
                return {"status": "CONFIRMED", "email": self.confirmed_email}
            if reply.button_id == "email_edit":
                await self.wa.send_text(self.recipient, EMAIL_EDIT_TEXT)
                self._count_wa()
                return {
                    "status": "EDIT_REQUESTED",
                    "hint": "wait for the corrected email, then confirm it again",
                }
            # Stale tap from an earlier confirmation message: keep waiting.
        return {"status": "NO_REPLY", "hint": "call again to keep waiting"}

    def mark_confirmed(self, email: str) -> None:
        """Out-of-band confirmation (chat loop consumed the button tap)."""
        self.confirmed_email = email
        self._bg(self.profile_store.upsert(self.recipient, email=email))

    # -- booking ------------------------------------------------------------

    async def book(
        self,
        start_local_iso: str,
        attendee_name: str,
        topic: str,
        email: str,
        book_anyway: bool = False,
    ) -> dict[str, Any]:
        email = (email or "").strip()
        if not email or not EMAIL_RE.fullmatch(email):
            return {"status": "EMAIL_REQUIRED",
                    "hint": "collect an email and confirm it on WhatsApp first"}
        if email != self.confirmed_email:
            return {"status": "EMAIL_NOT_CONFIRMED",
                    "hint": "confirm this exact email via confirm_email_on_whatsapp first"}
        if not book_anyway:
            existing = await self.booking_store.list_upcoming(self.recipient)
            if existing:
                return {
                    "status": "EXISTING_BOOKING",
                    "existing": [self._row_for_model(row) for row in existing],
                    "hint": "tell the caller; re-call with book_anyway=true to add another",
                }
        parsed = self._localize(start_local_iso)
        try:
            booking = await asyncio.to_thread(
                self.cal.create_booking,
                self.event_type_id,
                self._utc_iso(parsed),
                attendee_name or "WhatsApp Caller",
                self.timezone,
                email,
                f"+{self.recipient.lstrip('+')}",
                {"topic": topic[:200], "source": self.source},
            )
        except Exception as exc:
            logger.exception("booking failed")
            self.session_errors.append({"op": "book", "error": str(exc)[:200]})
            return {"status": "FAILED", "error": str(exc)[:200]}
        uid = booking.get("uid", "")
        self._bg(self.wa.send_text(
            self.recipient, format_confirmation(parsed, self.timezone, topic, uid)
        ))
        self._count_wa()
        self._bg(self.booking_store.add(self.recipient, uid, parsed.astimezone(dt.UTC), topic))
        self._bg(self.profile_store.upsert(
            self.recipient, name=attendee_name, email=email, timezone=self.timezone
        ))
        self.session_actions.append({
            "action": "booked", "uid": uid, "topic": topic,
            "when_local": parsed.strftime("%A, %d %B %Y at %I:%M %p"),
        })
        return {"status": "BOOKED", "uid": uid}

    # -- manage existing bookings --------------------------------------------

    def _row_for_model(self, row: dict[str, Any]) -> dict[str, str]:
        when_local = row["start_utc"].astimezone(self._tz)
        return {
            "uid": row["uid"],
            "when_local": when_local.strftime("%A, %d %B %Y at %I:%M %p"),
            "start_local_iso": when_local.strftime("%Y-%m-%dT%H:%M:%S"),
            "topic": row["topic"],
        }

    async def list_bookings(self) -> dict[str, Any]:
        rows = await self.booking_store.list_upcoming(self.recipient)
        return {"bookings": [self._row_for_model(row) for row in rows]}

    async def cancel(self, uid: str, reason: str = "") -> dict[str, Any]:
        try:
            await asyncio.to_thread(
                self.cal.cancel_booking, uid, reason or "cancelled by caller"
            )
        except Exception as exc:
            logger.exception("cancel failed")
            self.session_errors.append({"op": "cancel", "error": str(exc)[:200]})
            return {"status": "FAILED", "error": str(exc)[:200]}
        self._bg(self.booking_store.set_status(uid, "cancelled"))
        self._bg(self.wa.send_text(self.recipient, format_cancellation(uid)))
        self._count_wa()
        self.session_actions.append({"action": "cancelled", "uid": uid})
        return {"status": "CANCELLED", "uid": uid}

    async def reschedule(self, uid: str, new_start_local_iso: str) -> dict[str, Any]:
        parsed = self._localize(new_start_local_iso)
        try:
            moved = await asyncio.to_thread(
                self.cal.reschedule_booking, uid, self._utc_iso(parsed)
            )
        except Exception as exc:
            logger.exception("reschedule failed")
            self.session_errors.append({"op": "reschedule", "error": str(exc)[:200]})
            return {"status": "FAILED", "error": str(exc)[:200]}
        new_uid = moved.get("uid", "") or uid
        self._bg(self.booking_store.replace_uid(uid, new_uid, parsed.astimezone(dt.UTC)))
        self._bg(self.wa.send_text(
            self.recipient, format_reschedule(parsed, self.timezone, new_uid)
        ))
        self._count_wa()
        self.session_actions.append({
            "action": "rescheduled", "uid": new_uid,
            "when_local": parsed.strftime("%A, %d %B %Y at %I:%M %p"),
        })
        return {"status": "RESCHEDULED", "uid": new_uid}

    async def aclose(self) -> None:
        await self.booking_store.close()
        await self.profile_store.close()


async def create_booking_service(
    cal: CalClient,
    wa: WhatsAppClient,
    hub: WebhookHub,
    recipient: str,
    event_type_id: int,
    timezone: str,
    source: str,
) -> BookingService:
    """Construct + connect stores, load the caller profile, pre-seed the
    confirmed email from it (the prompt makes the agent verbally re-confirm
    a stored email before booking with it)."""
    from whatsapp_agent.config import get_settings

    db_url = get_settings().database_url
    profile_store = ProfileStore(db_url)
    booking_store = BookingStore(db_url)
    await profile_store.connect()
    await booking_store.connect()
    service = BookingService(
        cal, wa, hub, booking_store, profile_store,
        recipient, event_type_id, timezone, source,
    )
    service.profile = await profile_store.load(recipient)
    if service.profile and service.profile.get("email"):
        service.confirmed_email = service.profile["email"]
    return service
