"""Thin bridge: voiceagent AgentContext -> checkpointed BookingAgent.

The old fixed state machine lived here; it repeated canned lines on refusal
and couldn't detour. Dialogue now belongs to the LLM (graph.BookingAgent,
state in Neon Postgres); this class only maps tool starts to spoken status
pulses and pins the per-caller thread id.
"""

from __future__ import annotations

import logging
from typing import Any

from whatsapp_agent.agent.brain import BookingAgent
from whatsapp_agent.agent.prompts import DUAL_BRAIN_GREET_TRIGGER

logger = logging.getLogger("whatsapp_agent")

# Tool name -> speakable step for the live-commentary engine.
_STEP_NAMES = {
    "get_available_slots": "checking_the_calendar",
    "book_appointment": "booking_the_slot",
    "request_email_over_whatsapp": "sending_you_a_whatsapp_message",
    "confirm_email_on_whatsapp": "sending_the_confirmation_buttons",
    "list_my_bookings": "checking_your_bookings",
    "cancel_appointment": "cancelling_that",
    "reschedule_appointment": "moving_your_booking",
}


class BookingCall:
    def __init__(self, agent: BookingAgent, thread_id: str) -> None:
        self._agent = agent
        self.thread_id = thread_id
        # THIS call's turns, for the post-call recap (DB rows are
        # fire-and-forget and span every past session).
        self.session_turns: list[tuple[str, str]] = []

    async def handler(self, ctx: Any) -> str | None:
        async def status_cb(tool_name: str) -> None:
            await ctx.status(_STEP_NAMES.get(tool_name, tool_name))

        self.session_turns.append(("user", ctx.text))
        try:
            reply = await self._agent.respond(ctx.text, self.thread_id, status_cb)
        except Exception:
            logger.exception("booking agent turn failed")
            return "Sorry, I hit a snag on my side. Could you say that again?"
        if reply:
            self.session_turns.append(("assistant", reply))
        return reply

    async def greet(self) -> str:
        """First words after the callee picks up — LLM-generated so repeat
        callers get a natural 'welcome back' instead of a canned line."""
        return await self._agent.respond(DUAL_BRAIN_GREET_TRIGGER, self.thread_id)
