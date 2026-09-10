"""Example tools: plain Python functions, engine-neutral.

Referenced from examples/agents.yaml via dotted paths, and imported directly
by the Python example agents.
"""

from __future__ import annotations

import datetime as dt

from voiceagent import ToolContext, ToolFailure, tool

_FAKE_CALENDAR: dict[str, list[str]] = {
    "monday": ["10:00", "14:00"],
    "tuesday": ["09:00", "11:30", "16:00"],
    "wednesday": ["13:00"],
}


@tool(description="List available appointment slots for a weekday")
async def get_available_slots(ctx: ToolContext, weekday: str) -> str:
    slots = _FAKE_CALENDAR.get(weekday.lower())
    if slots is None:
        raise ToolFailure(f"I don't have a calendar for {weekday}.")
    return f"Available on {weekday}: {', '.join(slots)}" if slots else f"No slots on {weekday}."


@tool(description="Book an appointment slot for the caller")
async def book_appointment(ctx: ToolContext, weekday: str, time: str, name: str) -> str:
    slots = _FAKE_CALENDAR.get(weekday.lower(), [])
    if time not in slots:
        raise ToolFailure(f"{time} on {weekday} is not available.")
    slots.remove(time)
    booked_for = ctx.ids.user_id or name
    return f"Booked {weekday} {time} for {booked_for}."


@tool(description="Get today's date and the current time")
def current_datetime() -> str:  # sync tools work too
    return dt.datetime.now(dt.UTC).strftime("%A %Y-%m-%d %H:%M UTC")
