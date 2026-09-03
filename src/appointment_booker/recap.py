"""Post-call recap: summarize the session and WhatsApp it to the caller.

Runs AFTER the call ends, off the audio path. One cheap LLM call over the
in-memory session turns (never the DB — fire-and-forget rows may land after
hangup). Idempotent, and any failure is swallowed: a missing recap must
never break teardown.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
from typing import Any
from zoneinfo import ZoneInfo

from appointment_booker.prompts import CALL_RECAP_PROMPT
from appointment_booker.whatsapp_api import WhatsAppClient

logger = logging.getLogger("appointment_booker")


class RecapSender:
    def __init__(
        self,
        wa: WhatsAppClient,
        recipient: str,
        business_name: str,
        model_name: str | None = None,
        min_user_turns: int | None = None,
        llm: Any = None,  # injectable for tests; else built on first send
        meter: Any = None,  # optional UsageMeter for cost accounting
    ) -> None:
        from appointment_booker.config import get_settings

        settings = get_settings()
        self._wa = wa
        self._recipient = recipient
        self._business = business_name
        self._meter = meter
        self._model_name = model_name or settings.recap_model or settings.scheduler_model
        self._min_user_turns = (
            min_user_turns if min_user_turns is not None
            else settings.recap_min_user_turns
        )
        self._llm = llm
        self._sent = False

    async def send(
        self,
        turns: list[tuple[str, str]],
        session_actions: list[dict[str, str]],
        upcoming: list[dict[str, Any]] | None = None,
    ) -> bool:
        """True if a recap went out. Skips trivial calls (wrong numbers,
        instant hangups) and never sends twice per session."""
        if self._sent:
            return False
        user_turns = sum(1 for role, _ in turns if role == "user")
        if user_turns < self._min_user_turns:
            logger.info("recap skipped: %d user turn(s) < %d", user_turns, self._min_user_turns)
            return False
        self._sent = True
        try:
            if self._llm is None:
                from langchain_google_genai import ChatGoogleGenerativeAI

                self._llm = ChatGoogleGenerativeAI(model=self._model_name, temperature=0.3)
            transcript = "\n".join(f"{role}: {text}" for role, text in turns[-60:])
            facts = {
                "actions_this_call": session_actions,
                "upcoming_bookings": upcoming or [],
            }
            from appointment_booker.config import get_settings

            business_tz = ZoneInfo(get_settings().cal_timezone)
            system = CALL_RECAP_PROMPT.format(
                business_name=self._business,
                date=dt.datetime.now(business_tz).strftime("%d %B %Y"),
            )
            reply = await self._llm.ainvoke([
                ("system", system),
                ("user", f"Transcript:\n{transcript}\n\n"
                         f"Ground-truth booking facts:\n{json.dumps(facts, default=str)}"),
            ])
            if self._meter is not None and (
                usage := getattr(reply, "usage_metadata", None)
            ):
                self._meter.add("gemini_flash_recap", "input_tokens",
                                usage.get("input_tokens", 0))
                self._meter.add("gemini_flash_recap", "output_tokens",
                                usage.get("output_tokens", 0))
            text = self._extract_text(reply)
            if not text.strip():
                return False
            await self._wa.send_text(self._recipient, text.strip())
            if self._meter is not None:
                self._meter.add("whatsapp", "messages", 1)
            logger.info("call recap sent to %s", self._recipient)
            return True
        except Exception:
            logger.exception("recap send failed (ignored)")
            return False

    @staticmethod
    def _extract_text(message: Any) -> str:
        content = getattr(message, "content", message)
        if isinstance(content, str):
            return content
        if isinstance(content, list):  # Gemini 3.x part lists
            return "".join(
                part.get("text", "") if isinstance(part, dict) else str(part)
                for part in content
            )
        return str(content or "")
