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
from typing import Any

from pydantic import BaseModel

from whatsapp_agent.capabilities.booking.service import BookingService

logger = logging.getLogger("whatsapp_agent")


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
    book_anyway: bool = False


class EmailWaitArgs(BaseModel):
    wait_seconds: float | None = None  # model may omit or send null


class EmailConfirmArgs(BaseModel):
    email: str


class BookingRefArgs(BaseModel):
    booking_uid: str
    reason: str = ""


class RescheduleArgs(BaseModel):
    booking_uid: str
    new_start_local_iso: dt.datetime


def build_native_tools(
    service: BookingService,
    end_call_cb: Any = None,
) -> dict[str, dict[str, Any]]:
    """Returns {tool_name: {"declaration": <Gemini functionDeclaration>,
    "handler": async fn(args) -> dict}} for GeminiLiveProxy native_tools.

    Handlers are thin arg-validation wrappers over BookingService so both
    brain modes share one implementation of every booking operation.
    """

    async def end_call(args: dict[str, Any]) -> dict[str, Any]:
        if end_call_cb is None:
            return {"status": "UNSUPPORTED"}
        # Background so the model still speaks its goodbye this turn; the
        # callback waits for that audio to drain before tearing down the leg.
        asyncio.get_running_loop().create_task(end_call_cb())
        return {"status": "ENDING", "note": "call ends after the goodbye"}

    async def get_available_slots(args: dict[str, Any]) -> dict[str, Any]:
        query = SlotQueryArgs.model_validate(args)
        slots = await asyncio.to_thread(
            service.cal.get_slots, service.event_type_id,
            query.start_date, query.end_date, service.timezone,
        )
        # Compact for prompt economy: 8 slots/day max, local HH:MM only.
        return {
            "timezone": service.timezone,
            "slots": {
                day: [entry["start"][11:16] for entry in entries[:8]]
                for day, entries in slots.items()
            },
        }

    async def book_appointment(args: dict[str, Any]) -> dict[str, Any]:
        request = BookingArgs.model_validate(args)
        return await service.book(
            request.start_local_iso.isoformat(),
            request.attendee_name,
            request.topic,
            request.email,
            book_anyway=request.book_anyway,
        )

    async def request_email_over_whatsapp(args: dict[str, Any]) -> dict[str, Any]:
        wait_s = min(max(EmailWaitArgs.model_validate(args).wait_seconds or 45.0, 10.0), 90.0)
        return await service.request_email(wait_s)

    async def confirm_email(args: dict[str, Any]) -> dict[str, Any]:
        request = EmailConfirmArgs.model_validate(args)
        return await service.confirm_email_by_voice(request.email)

    async def confirm_email_on_whatsapp(args: dict[str, Any]) -> dict[str, Any]:
        request = EmailConfirmArgs.model_validate(args)
        sent = await service.send_email_confirmation(request.email)
        if sent.get("status") != "CONFIRMATION_SENT":
            return sent
        return await service.wait_email_confirmation(40.0)

    async def list_my_bookings(args: dict[str, Any]) -> dict[str, Any]:
        return await service.list_bookings()

    async def cancel_appointment(args: dict[str, Any]) -> dict[str, Any]:
        request = BookingRefArgs.model_validate(args)
        return await service.cancel(request.booking_uid, request.reason)

    async def reschedule_appointment(args: dict[str, Any]) -> dict[str, Any]:
        request = RescheduleArgs.model_validate(args)
        return await service.reschedule(
            request.booking_uid, request.new_start_local_iso.isoformat()
        )

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
                               "and time AND their email is confirmed. "
                               "start_local_iso is local time, e.g. "
                               "2026-08-29T16:00:00. Returns EMAIL_REQUIRED / "
                               "EMAIL_NOT_CONFIRMED / EXISTING_BOOKING when "
                               "preconditions are missing.",
                "parameters": {
                    "type": "OBJECT",
                    "properties": {
                        "start_local_iso": {"type": "STRING"},
                        "attendee_name": {"type": "STRING"},
                        "topic": {"type": "STRING"},
                        "email": {"type": "STRING",
                                  "description": "the WhatsApp-confirmed email"},
                        "book_anyway": {"type": "BOOLEAN",
                                        "description": "true only after the caller "
                                                       "chose to keep an existing "
                                                       "booking AND add this one"},
                    },
                    "required": ["start_local_iso", "attendee_name", "topic", "email"],
                },
            },
            "handler": book_appointment,
        },
        "confirm_email": {
            "declaration": {
                "name": "confirm_email",
                "description": "Lock in the caller's email for booking — the "
                               "PREFERRED way on a voice call. Call this only "
                               "AFTER reading the email back aloud (spell the "
                               "part before the @ letter by letter) and the "
                               "caller confirmed it. No WhatsApp message is "
                               "sent. Returns CONFIRMED or INVALID_EMAIL "
                               "(apologize and ask again).",
                "parameters": {
                    "type": "OBJECT",
                    "properties": {
                        "email": {"type": "STRING"},
                    },
                    "required": ["email"],
                },
            },
            "handler": confirm_email,
        },
        "request_email_over_whatsapp": {
            "declaration": {
                "name": "request_email_over_whatsapp",
                "description": "FALLBACK ONLY. Send the caller a WhatsApp text "
                               "asking for their email and wait for the reply. "
                               "Use only if the caller asks to type it, or you "
                               "cannot make out the email after two careful "
                               "read-backs. Prefer confirm_email by voice.",
                "parameters": {
                    "type": "OBJECT",
                    "properties": {
                        "wait_seconds": {"type": "NUMBER"},
                    },
                },
            },
            "handler": request_email_over_whatsapp,
        },
        "confirm_email_on_whatsapp": {
            "declaration": {
                "name": "confirm_email_on_whatsapp",
                "description": "FALLBACK. Send the collected email back to the "
                               "caller on WhatsApp with Confirm/Edit buttons "
                               "and wait for their tap. Use only if the caller "
                               "asked to confirm in writing. Booking is blocked "
                               "until this returns CONFIRMED. On NO_REPLY you "
                               "may call it again to keep waiting.",
                "parameters": {
                    "type": "OBJECT",
                    "properties": {
                        "email": {"type": "STRING"},
                    },
                    "required": ["email"],
                },
            },
            "handler": confirm_email_on_whatsapp,
        },
        "list_my_bookings": {
            "declaration": {
                "name": "list_my_bookings",
                "description": "List the caller's upcoming appointments "
                               "(uid, local time, topic). Call this FIRST "
                               "when they ask to change, cancel or check a "
                               "booking.",
                "parameters": {"type": "OBJECT", "properties": {}},
            },
            "handler": list_my_bookings,
        },
        "cancel_appointment": {
            "declaration": {
                "name": "cancel_appointment",
                "description": "Cancel a booking by uid. Read the booking "
                               "back and get an explicit yes BEFORE calling.",
                "parameters": {
                    "type": "OBJECT",
                    "properties": {
                        "booking_uid": {"type": "STRING"},
                        "reason": {"type": "STRING"},
                    },
                    "required": ["booking_uid"],
                },
            },
            "handler": cancel_appointment,
        },
        "reschedule_appointment": {
            "declaration": {
                "name": "reschedule_appointment",
                "description": "Move a booking to a new local time. Needs an "
                               "explicit yes to the new exact slot first. "
                               "Returns the NEW booking uid.",
                "parameters": {
                    "type": "OBJECT",
                    "properties": {
                        "booking_uid": {"type": "STRING"},
                        "new_start_local_iso": {"type": "STRING"},
                    },
                    "required": ["booking_uid", "new_start_local_iso"],
                },
            },
            "handler": reschedule_appointment,
        },
        "end_call": {
            "declaration": {
                "name": "end_call",
                "description": "Hang up the phone call. Call this ONLY after "
                               "the caller confirmed they need nothing else, "
                               "together with your sign-off in the SAME turn "
                               "(the goodbye plays in full first). Do NOT call "
                               "it right after a booking — first ask if there "
                               "is anything else. Or call it immediately if "
                               "the caller asks to end or cut the call.",
                "parameters": {"type": "OBJECT", "properties": {}},
            },
            "handler": end_call,
        },
    }
