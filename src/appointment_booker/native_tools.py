"""Native tools for SINGLE-BRAIN mode: Gemini Live calls these directly.

One LLM in the loop — the live voice model checks the calendar and books by
itself, which removes the entire second model round trip of dual-brain mode
(~3-6 s per reply). Handlers are async-native (no worker-thread bridging);
blocking Cal.com HTTP runs via asyncio.to_thread.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import logging
import re
from typing import Any
from zoneinfo import ZoneInfo

from pydantic import BaseModel

from appointment_booker.cal_client import CalClient
from appointment_booker.webhooks import WebhookHub
from appointment_booker.whatsapp_api import WhatsAppClient

logger = logging.getLogger("appointment_booker")

_EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+\.[\w.]+")


# Tool arguments come from the LLM — validate them like any untrusted input.
# A ValidationError propagates back to Gemini as a tool error, and the model
# self-corrects on the next attempt.
class SlotQueryArgs(BaseModel):
    start_date: dt.date
    end_date: dt.date


class BookingArgs(BaseModel):
    start_local_iso: dt.datetime
    attendee_name: str = ""
    topic: str = ""
    email: str = ""


class EmailWaitArgs(BaseModel):
    wait_seconds: float | None = None  # model may omit or send null


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


class TranscriptStore:
    """Async, non-blocking transcript persistence for single-brain mode.

    Same ``voiceagent_turns`` table the dual-brain agent uses, so both modes
    share one conversation history per caller. Writes are fire-and-forget:
    the call never waits on Neon.
    """

    def __init__(self, thread_id: str, db_url: str) -> None:
        self._thread_id = thread_id
        self._db_url = db_url
        self._db: Any = None

    async def connect(self) -> None:
        import psycopg

        try:
            self._db = await psycopg.AsyncConnection.connect(
                self._db_url, autocommit=True, connect_timeout=10
            )
            await self._db.execute(
                "CREATE TABLE IF NOT EXISTS voiceagent_turns ("
                "id bigserial PRIMARY KEY, thread_id text NOT NULL, "
                "role text NOT NULL, content text NOT NULL, "
                "created_at timestamptz NOT NULL DEFAULT now())"
            )
        except Exception as exc:
            logger.warning("transcript store unavailable (memory-only call): %s", exc)
            self._db = None

    async def save(self, role: str, text: str) -> None:
        """Signature matches GeminiLiveProxy's on_transcription callback."""
        if self._db is None:
            return
        asyncio.get_running_loop().create_task(self._insert(role, text))

    async def load_recent(self, limit: int = 30) -> list[tuple[str, str]]:
        """(role, content) rows, oldest first — cross-channel history for
        seeding a new voice session's context."""
        if self._db is None:
            return []
        try:
            cursor = await self._db.execute(
                "SELECT role, content FROM voiceagent_turns "
                "WHERE thread_id = %s ORDER BY id DESC LIMIT %s",
                (self._thread_id, limit),
            )
            return list(reversed(await cursor.fetchall()))
        except Exception as exc:
            logger.warning("history load failed: %s", exc)
            return []

    async def _insert(self, role: str, text: str) -> None:
        # Neon suspends idle connections mid-call; reconnect once and retry.
        for attempt in (1, 2):
            try:
                await self._db.execute(
                    "INSERT INTO voiceagent_turns (thread_id, role, content) VALUES (%s, %s, %s)",
                    (self._thread_id, role, text),
                )
                return
            except Exception as exc:
                if attempt == 2:
                    logger.warning("transcript insert failed (dropped): %s", exc)
                    return
                logger.info("transcript conn stale, reconnecting: %s", exc)
                await self.connect()
                if self._db is None:
                    return

    async def close(self) -> None:
        if self._db is not None:
            await self._db.close()


class TextBridge:
    """Chat<->call sync during a live call.

    SINGLE consumer of inbound WhatsApp texts while a call is active:
    every message is (a) injected into the live Gemini session so the voice
    agent reads and reacts to it, (b) checked for an email address to
    satisfy request_email_over_whatsapp, and (c) persisted to the shared
    transcript thread. Without this, the email tool and any chat handler
    race each other on the same queue.
    """

    def __init__(self, hub: WebhookHub, store: Any = None) -> None:
        self._hub = hub
        self._store = store
        self._inject: Any = None  # async fn(text) -> None, set via attach()
        self._task: asyncio.Task | None = None
        self._email: str | None = None
        self._email_event = asyncio.Event()

    def attach(self, inject_cb: Any) -> None:
        self._inject = inject_cb

    def start(self) -> None:
        self._task = asyncio.get_running_loop().create_task(self._loop())

    async def _loop(self) -> None:
        while True:
            try:
                msg = await self._hub.wait_text(timeout_s=3600)
            except TimeoutError:
                continue
            text = msg.text.strip()
            if not text:
                continue
            if match := _EMAIL_RE.search(text):
                self._email = match.group()
                self._email_event.set()
            if self._store is not None:
                await self._store.save("user", f"[via chat] {text}")
            if self._inject is not None:
                try:
                    await self._inject(text)
                except Exception:
                    logger.exception("mid-call text injection failed")

    async def wait_email(self, timeout_s: float) -> str | None:
        try:
            await asyncio.wait_for(self._email_event.wait(), timeout_s)
        except TimeoutError:
            return None
        self._email_event.clear()
        return self._email

    def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()


def build_native_tools(
    cal: CalClient,
    wa: WhatsAppClient,
    hub: WebhookHub,
    recipient: str,
    event_type_id: int,
    timezone: str = "Asia/Kolkata",
    end_call_cb: Any = None,
    text_bridge: TextBridge | None = None,
) -> dict[str, dict[str, Any]]:
    """Returns {tool_name: {"declaration": <Gemini functionDeclaration>,
    "handler": async fn(args) -> dict}} for GeminiLiveProxy native_tools."""
    tz = ZoneInfo(timezone)

    async def end_call(args: dict[str, Any]) -> dict[str, Any]:
        if end_call_cb is None:
            return {"status": "UNSUPPORTED"}

        async def delayed() -> None:
            # Grace period so the goodbye audio finishes playing before the
            # WebRTC leg tears down.
            await asyncio.sleep(3.5)
            await end_call_cb()

        asyncio.get_running_loop().create_task(delayed())
        return {"status": "ENDING", "note": "call will end in a few seconds"}

    async def get_available_slots(args: dict[str, Any]) -> dict[str, Any]:
        query = SlotQueryArgs.model_validate(args)
        slots = await asyncio.to_thread(
            cal.get_slots, event_type_id, query.start_date, query.end_date, timezone
        )
        # Compact for prompt economy: 8 slots/day max, local HH:MM only.
        return {
            "timezone": timezone,
            "slots": {
                day: [entry["start"][11:16] for entry in entries[:8]]
                for day, entries in slots.items()
            },
        }

    async def book_appointment(args: dict[str, Any]) -> dict[str, Any]:
        request = BookingArgs.model_validate(args)
        parsed = request.start_local_iso
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=tz)
        start_utc = parsed.astimezone(dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        # Event type requires an email; placeholder unblocks voice booking
        # (verified working) — real confirmation goes out on WhatsApp.
        email = request.email or f"wa{recipient.lstrip('+')}@example.com"
        try:
            booking = await asyncio.to_thread(
                cal.create_booking,
                event_type_id,
                start_utc,
                request.attendee_name or "WhatsApp Caller",
                timezone,
                email,
                f"+{recipient.lstrip('+')}",
                {"topic": request.topic[:200], "source": "voice-single-brain"},
            )
        except Exception as exc:
            logger.exception("booking failed")
            return {"status": "FAILED", "error": str(exc)[:200]}
        uid = booking.get("uid", "")
        asyncio.get_running_loop().create_task(wa.send_text(
            recipient,
            format_confirmation(parsed, timezone, request.topic, uid),
        ))
        return {"status": "BOOKED", "uid": uid}

    async def request_email_over_whatsapp(args: dict[str, Any]) -> dict[str, Any]:
        await wa.send_text(
            recipient,
            "📧 To finish your booking, please reply here with your email address.",
        )
        wait_s = min(max(EmailWaitArgs.model_validate(args).wait_seconds or 45.0, 10.0), 90.0)
        # The bridge owns the text queue during calls (single consumer);
        # it hands us the email when the reply lands.
        assert text_bridge is not None, "call setup must provide a TextBridge"
        email = await text_bridge.wait_email(wait_s)
        return {"email": email} if email else {"status": "NO_REPLY"}

    return {
        "get_available_slots": {
            "declaration": {
                "name": "get_available_slots",
                "description": "Fetch open appointment slots between two dates "
                               "(inclusive), in the organizer's local timezone. "
                               "Never invent slots — always trust this output.",
                "parameters": {
                    "type": "OBJECT",
                    "properties": {
                        "start_date": {"type": "STRING", "description": "YYYY-MM-DD"},
                        "end_date": {"type": "STRING", "description": "YYYY-MM-DD"},
                    },
                    "required": ["start_date", "end_date"],
                },
            },
            "handler": get_available_slots,
        },
        "book_appointment": {
            "declaration": {
                "name": "book_appointment",
                "description": "Book the confirmed slot. Call ONLY after the "
                               "caller explicitly said yes to this exact day "
                               "and time. start_local_iso is local time, e.g. "
                               "2026-08-29T16:00:00.",
                "parameters": {
                    "type": "OBJECT",
                    "properties": {
                        "start_local_iso": {"type": "STRING"},
                        "attendee_name": {"type": "STRING"},
                        "topic": {"type": "STRING"},
                        "email": {"type": "STRING", "description": "empty if not collected"},
                    },
                    "required": ["start_local_iso", "attendee_name", "topic"],
                },
            },
            "handler": book_appointment,
        },
        "request_email_over_whatsapp": {
            "declaration": {
                "name": "request_email_over_whatsapp",
                "description": "Send the caller a WhatsApp text asking for "
                               "their email and wait for the reply. Tell the "
                               "caller you've sent it while waiting. Booking "
                               "works without email too.",
                "parameters": {
                    "type": "OBJECT",
                    "properties": {
                        "wait_seconds": {"type": "NUMBER"},
                    },
                },
            },
            "handler": request_email_over_whatsapp,
        },
        "end_call": {
            "declaration": {
                "name": "end_call",
                "description": "Hang up the phone call. Call this AFTER "
                               "saying goodbye when the conversation is "
                               "finished, or immediately when the caller "
                               "asks you to end or cut the call.",
                "parameters": {"type": "OBJECT", "properties": {}},
            },
            "handler": end_call,
        },
    }
