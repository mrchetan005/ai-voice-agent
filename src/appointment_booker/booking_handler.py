"""Thin bridge: voiceagent AgentContext -> checkpointed BookingAgent.

The old fixed state machine lived here; it repeated canned lines on refusal
and couldn't detour. Dialogue now belongs to the LLM (graph.BookingAgent,
state in Neon Postgres); this class only maps tool starts to spoken status
pulses and pins the per-caller thread id.
"""

from __future__ import annotations

import logging
from typing import Any

from appointment_booker.graph import BookingAgent
from appointment_booker.prompts import DUAL_BRAIN_GREET_TRIGGER

logger = logging.getLogger("appointment_booker")

# Tool name -> speakable step for the live-commentary engine.
_STEP_NAMES = {
    "get_available_slots": "checking_the_calendar",
    "book_appointment": "booking_the_slot",
    "request_email_over_whatsapp": "sending_you_a_whatsapp_message",
}


class BookingCall:
    def __init__(self, agent: BookingAgent, thread_id: str) -> None:
        self._agent = agent
        self.thread_id = thread_id

    async def handler(self, ctx: Any) -> str | None:
        async def status_cb(tool_name: str) -> None:
            await ctx.status(_STEP_NAMES.get(tool_name, tool_name))

        try:
            return await self._agent.respond(ctx.text, self.thread_id, status_cb)
        except Exception:
            logger.exception("booking agent turn failed")
            return "Sorry, I hit a snag on my side. Could you say that again?"

    async def greet(self) -> str:
        """First words after the callee picks up — LLM-generated so repeat
        callers get a natural 'welcome back' instead of a canned line."""
        return await self._agent.respond(DUAL_BRAIN_GREET_TRIGGER, self.thread_id)
