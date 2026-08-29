"""Conversational booking brain: ONE LangGraph agent, two channels.

Used by: WhatsApp CHAT mode (channel="chat") and the DUAL-BRAIN voice
option (channel="voice", behind Gemini Live's send_to_agent tool).

Design:
* LLM-driven dialogue (no fixed state machine) — adapts on refusals
  instead of repeating canned lines.
* Hot loop runs on an in-memory checkpointer (zero DB latency per turn);
  turns persist to Postgres (Neon) asynchronously and threads are seeded
  from Neon once per caller, so restarts still remember people.
* thread_id = caller's WhatsApp number — shared with voice transcripts for
  cross-channel memory.

Offline sanity check (needs DATABASE_URL + GOOGLE_API_KEY + CAL keys):
    uv run --env-file .env python -m appointment_booker.graph "hi, I'd like a meeting tomorrow"
"""

from __future__ import annotations

import asyncio
import contextlib
import datetime as dt
import json
import logging
import os
import re
import sys
import time
from collections.abc import Awaitable, Callable
from typing import Any
from zoneinfo import ZoneInfo

import psycopg
from langchain_core.messages import AIMessage, HumanMessage
from langchain_core.tools import tool
from langchain_google_genai import ChatGoogleGenerativeAI
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.prebuilt import create_react_agent

from appointment_booker.cal_client import CalClient
from appointment_booker.prompts import (
    AVAILABILITY_GUIDE,
    CHAT_EMAIL_NOTE,
    CHAT_RULES,
    VOICE_DELIVERY_NOTE,
    VOICE_RULES,
)
from appointment_booker.webhooks import WebhookHub
from appointment_booker.whatsapp_api import WhatsAppClient

logger = logging.getLogger("appointment_booker")

_EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+\.[\w.]+")


class BookingAgent:
    """Checkpointed conversational agent; one instance per process,
    one thread_id per caller."""

    def __init__(
        self,
        cal: CalClient,
        event_type_id: int,
        wa: WhatsAppClient,
        hub: WebhookHub,
        recipient: str,
        timezone: str = "Asia/Kolkata",
        db_url: str | None = None,
        model: str | None = None,
        business_name: str = "our office",
        channel: str = "voice",  # "voice" (call relay) or "chat" (WA text)
    ) -> None:
        self._channel = channel
        self._cal = cal
        self._event_type_id = event_type_id
        self._tz = ZoneInfo(timezone)
        self._tz_name = timezone
        self._db_url = db_url or os.environ["DATABASE_URL"]
        self._agent: Any = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._persist_task: asyncio.Task[None] | None = None
        self._snapshot_task: asyncio.Task[None] | None = None
        self._slots_snapshot = ""
        # One turn at a time per caller: Gemini Live can fire a second
        # send_to_agent while the first is mid-tool; concurrent runs on the
        # same checkpointer thread corrupt state (dangling tool_calls).
        self._thread_locks: dict[str, asyncio.Lock] = {}

        @tool
        def get_available_slots(start_date: str, end_date: str) -> str:
            """Fetch open appointment slots between two dates (YYYY-MM-DD,
            inclusive), local timezone. Returns JSON keyed by date. Fast —
            call whenever you need real availability. Never invent slots."""
            slots = cal.get_slots(
                event_type_id,
                dt.date.fromisoformat(start_date),
                dt.date.fromisoformat(end_date),
                timezone,
            )
            # Trim to keep the context small: at most 6 slots per day.
            return json.dumps({day: entries[:6] for day, entries in slots.items()})

        @tool
        def book_appointment(
            start_local_iso: str, attendee_name: str, topic: str, email: str = ""
        ) -> str:
            """Book the confirmed slot. start_local_iso must be the exact
            slot start copied from get_available_slots output. Call ONLY
            after the caller clearly said yes to this specific time.
            Leave email empty if not collected."""
            parsed = dt.datetime.fromisoformat(start_local_iso)
            if parsed.tzinfo is None:
                # Snapshot times are org-local; never trust the OS timezone.
                parsed = parsed.replace(tzinfo=self._tz)
            start_utc = parsed.astimezone(dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
            # This event type's booking fields REQUIRE an email (verified:
            # {email}error_required_field). Placeholder keeps voice booking
            # unblocked; the real confirmation goes out on WhatsApp.
            # ".invalid" TLD is rejected by Cal's validator; example.com passes.
            effective_email = email or f"wa{recipient.lstrip('+')}@example.com"
            try:
                booking = cal.create_booking(
                    event_type_id,
                    start_utc,
                    attendee_name,
                    timezone,
                    effective_email,
                    f"+{recipient.lstrip('+')}",
                    metadata={"topic": topic[:200], "source": "whatsapp-voice-agent"},
                )
            except Exception as exc:  # LLM adapts to the failure text
                return f"BOOKING_FAILED: {str(exc)[:200]}"
            uid = booking.get("uid", "")
            from appointment_booker.native_tools import format_confirmation

            self._fire_and_forget(
                wa.send_text(
                    recipient,
                    format_confirmation(parsed, timezone, topic, uid),
                )
            )
            return f"BOOKED uid={uid}. Confirmation sent on WhatsApp."

        @tool
        def request_email_over_whatsapp(wait_seconds: int = 60) -> str:
            """Send the caller a WhatsApp text asking for their email and
            wait for the reply. Use while telling the caller you've sent it.
            Returns the email, or NO_REPLY if none arrives in time."""
            self._run_on_loop(
                wa.send_text(
                    recipient,
                    "📧 To finish your booking, please reply here with your email address.",
                )
            )
            stop_at = time.monotonic() + min(max(wait_seconds, 10), 120)
            while (remaining := stop_at - time.monotonic()) > 0:
                try:
                    msg = self._run_on_loop(hub.wait_text(timeout_s=remaining))
                except Exception:
                    return "NO_REPLY"
                if match := _EMAIL_RE.search(msg.text):
                    return match.group()
            return "NO_REPLY"

        self._llm = ChatGoogleGenerativeAI(
            model=model or os.environ.get("SCHEDULER_MODEL", "gemini-3.6-flash"),
            temperature=0.6,
        )
        if channel == "chat":
            # Chat: user can just type their email — the wait-for-reply tool
            # would fight the chat loop for the same message queue.
            self._tools = [get_available_slots, book_appointment]
            self._prompt = (
                CHAT_RULES.format(business_name=business_name)
                + AVAILABILITY_GUIDE
                + CHAT_EMAIL_NOTE
            )
        else:
            self._tools = [
                get_available_slots, book_appointment, request_email_over_whatsapp,
            ]
            self._prompt = (
                VOICE_RULES.format(business_name=business_name)
                + AVAILABILITY_GUIDE
                + VOICE_DELIVERY_NOTE
            )

    # -- thread bridging (tools run in LangGraph's worker thread) -------------

    def _run_on_loop(self, coro: Awaitable[Any]) -> Any:
        assert self._loop is not None
        return asyncio.run_coroutine_threadsafe(coro, self._loop).result(timeout=150)

    def _fire_and_forget(self, coro: Awaitable[Any]) -> None:
        assert self._loop is not None
        asyncio.run_coroutine_threadsafe(coro, self._loop)

    # -- lifecycle ---------------------------------------------------------------

    # Latency design: the LIVE loop runs on an in-memory checkpointer (zero
    # DB round trips between hearing and speaking). Persistence to Neon is a
    # background queue — fire-and-forget after each turn — and threads are
    # seeded FROM Neon once per process, so restarts still remember callers.
    _TABLE_SQL = (
        "CREATE TABLE IF NOT EXISTS voiceagent_turns ("
        "id bigserial PRIMARY KEY, thread_id text NOT NULL, "
        "role text NOT NULL, content text NOT NULL, "
        "created_at timestamptz NOT NULL DEFAULT now())"
    )
    _INDEX_SQL = (
        "CREATE INDEX IF NOT EXISTS idx_voiceagent_turns_thread "
        "ON voiceagent_turns (thread_id, id)"
    )

    async def start(self) -> None:
        self._loop = asyncio.get_running_loop()
        self._agent = create_react_agent(
            self._llm, self._tools, prompt=self._prompt, checkpointer=InMemorySaver()
        )
        self._seeded: set[str] = set()
        self._persist_queue: asyncio.Queue[tuple[str, str, str]] = asyncio.Queue(maxsize=256)
        self._db: psycopg.AsyncConnection | None = None
        try:
            self._db = await psycopg.AsyncConnection.connect(
                self._db_url, autocommit=True, connect_timeout=10
            )
            await self._db.execute(self._TABLE_SQL)
            await self._db.execute(self._INDEX_SQL)
        except Exception as exc:
            # DB down must never block calls: run memory-only, warn loudly.
            logger.warning("postgres unavailable, running memory-only: %s", exc)
            self._db = None
        self._persist_task = asyncio.create_task(self._persist_loop())
        # Availability prefetch: most turns are slot questions; with a fresh
        # snapshot in the prompt the model answers in ONE call instead of
        # tool-call -> fetch -> second call (cuts ~2-3 s per turn).
        self._slots_snapshot = ""
        self._snapshot_task = asyncio.create_task(self._refresh_slots_loop())
        logger.info("booking agent ready (in-memory hot path, async postgres persistence)")

    async def _refresh_slots_loop(self) -> None:
        while True:
            try:
                today = dt.datetime.now(self._tz).date()
                slots = await asyncio.to_thread(
                    self._cal.get_slots,
                    self._event_type_id,
                    today + dt.timedelta(days=1),
                    today + dt.timedelta(days=7),
                    self._tz_name,
                )
                days = []
                for day, entries in list(slots.items())[:7]:
                    times = ",".join(
                        e["start"][11:16] for e in entries[:8]
                    )
                    days.append(f"{day}: {times}")
                self._slots_snapshot = "; ".join(days) or "no open slots next 7 days"
                logger.debug("slots snapshot refreshed (%d days)", len(days))
            except Exception as exc:
                logger.warning("slots snapshot refresh failed: %s", exc)
            await asyncio.sleep(120)

    async def stop(self) -> None:
        if self._snapshot_task is not None:
            self._snapshot_task.cancel()
        if self._persist_task is not None:
            # Drain what's queued before shutting down.
            await asyncio.sleep(0)
            while not self._persist_queue.empty():
                await asyncio.sleep(0.05)
            self._persist_task.cancel()
        if self._db is not None:
            await self._db.close()

    async def _persist_loop(self) -> None:
        while True:
            thread_id, role, content = await self._persist_queue.get()
            if self._db is None:
                continue
            # Neon suspends idle connections; reconnect once and retry.
            for attempt in (1, 2):
                try:
                    await self._db.execute(
                        "INSERT INTO voiceagent_turns (thread_id, role, content) "
                        "VALUES (%s, %s, %s)",
                        (thread_id, role, content),
                    )
                    break
                except Exception as exc:
                    if attempt == 2:
                        logger.warning("turn persistence failed (dropped): %s", exc)
                        break
                    try:
                        self._db = await psycopg.AsyncConnection.connect(
                            self._db_url, autocommit=True, connect_timeout=10
                        )
                    except Exception as reconnect_exc:
                        logger.warning("persist reconnect failed: %s", reconnect_exc)
                        break

    async def _seed_thread(self, thread_id: str, config: dict[str, Any]) -> None:
        """Load prior turns from Neon into the in-memory thread — once per
        process per caller. Keeps cross-restart memory without paying DB
        latency on every turn."""
        self._seeded.add(thread_id)
        if self._db is None:
            return
        try:
            cursor = await self._db.execute(
                "SELECT role, content FROM voiceagent_turns "
                "WHERE thread_id = %s ORDER BY id DESC LIMIT 40",
                (thread_id,),
            )
            rows = list(reversed(await cursor.fetchall()))
        except Exception as exc:
            logger.warning("thread seed failed, starting fresh: %s", exc)
            return
        if not rows:
            return
        messages = [
            HumanMessage(content=content) if role == "user" else AIMessage(content=content)
            for role, content in rows
        ]
        await self._agent.aupdate_state(config, {"messages": messages})
        logger.info("seeded thread %s with %d prior turns", thread_id, len(rows))

    # -- one conversational turn ------------------------------------------------

    async def respond(
        self,
        text: str,
        thread_id: str,
        status_cb: Callable[[str], Awaitable[None]] | None = None,
    ) -> str:
        """Run one turn; returns the text to speak. Tool starts surface as
        status callbacks -> live spoken commentary."""
        assert self._agent is not None, "call start() first"
        now = dt.datetime.now(self._tz)
        snapshot = (
            f" [availability next 7 days, {self._tz_name} local times: "
            f"{self._slots_snapshot}]" if self._slots_snapshot else ""
        )
        stamped = f"[{now.strftime('%A %Y-%m-%d %H:%M')} {self._tz_name}]{snapshot} {text}"
        config = {"configurable": {"thread_id": thread_id}}
        lock = self._thread_locks.setdefault(thread_id, asyncio.Lock())
        started = time.monotonic()
        async with lock:
            if thread_id not in self._seeded:
                await self._seed_thread(thread_id, config)
            await self._heal_dangling_tool_calls(config)
            final = ""
            try:
                async with asyncio.timeout(60):
                    async for event in self._agent.astream_events(
                        {"messages": [("user", stamped)]}, config=config, version="v2"
                    ):
                        kind = event.get("event")
                        if kind == "on_chat_model_end":
                            # Gemini 3.x content is a list of parts, not a
                            # plain string; the LAST model turn is the reply
                            # (earlier ones are tool-call decisions).
                            reply = self._extract_text(event.get("data", {}).get("output"))
                            if reply:
                                final = reply
                        elif kind == "on_tool_start" and status_cb is not None:
                            await status_cb(event.get("name", "working"))
            except TimeoutError:
                # Timed-out run leaves a dangling tool_call; next turn heals it.
                logger.warning("agent turn timed out after 60s (thread=%s)", thread_id)
                return (
                    "Sorry, that's taking longer than expected on my side. "
                    "Could you give me a moment and say that again?"
                )
            finally:
                logger.info(
                    "agent turn took %.1fs (thread=%s)", time.monotonic() - started, thread_id
                )
            # Persistence is fire-and-forget: the reply is already on its way
            # to the voice engine before these rows ever reach Neon.
            with contextlib.suppress(asyncio.QueueFull):
                self._persist_queue.put_nowait((thread_id, "user", text))
                if final:
                    self._persist_queue.put_nowait((thread_id, "assistant", final))
            return final.strip()

    async def _heal_dangling_tool_calls(self, config: dict[str, Any]) -> None:
        """Repair a thread whose last checkpoint is an AIMessage with
        unanswered tool_calls (a cancelled/timed-out/barged-in turn).
        Without this, every later turn raises
        'Found AIMessages with tool_calls that do not have a corresponding
        ToolMessage' — permanently bricking the caller's thread."""
        from langchain_core.messages import ToolMessage

        state = await self._agent.aget_state(config)
        messages = (state.values or {}).get("messages", []) if state else []
        if not messages:
            return
        last = messages[-1]
        tool_calls = getattr(last, "tool_calls", None) or []
        if not tool_calls:
            return
        await self._agent.aupdate_state(config, {"messages": [
            ToolMessage(
                content="(interrupted — the caller spoke before this finished; "
                        "re-run the tool if still relevant)",
                tool_call_id=tc["id"],
            )
            for tc in tool_calls
        ]})
        logger.info("healed %d dangling tool call(s)", len(tool_calls))

    @staticmethod
    def _extract_text(message: Any) -> str:
        content = getattr(message, "content", None)
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            parts: list[str] = []
            for part in content:
                if isinstance(part, str):
                    parts.append(part)
                elif isinstance(part, dict) and part.get("type") == "text":
                    parts.append(part.get("text", ""))
            return "".join(parts)
        return ""


async def _self_check(utterance: str) -> int:
    logging.basicConfig(level=logging.INFO)
    cal = CalClient()
    wa = WhatsAppClient()
    hub = WebhookHub()  # not started: email tool unused in this check
    agent = BookingAgent(
        cal,
        int(os.environ["CAL_EVENT_TYPE_ID"]),
        wa,
        hub,
        os.environ["WHATSAPP_RECIPIENT"],
        timezone=os.environ.get("CAL_TIMEZONE", "Asia/Kolkata"),
    )
    await agent.start()
    try:
        async def status(name: str) -> None:
            print(f"  [status] {name}")

        reply = await agent.respond(utterance, thread_id="self-check", status_cb=status)
        print(f"agent: {reply}")
        reply2 = await agent.respond(
            "actually no, that doesn't work for me", "self-check", status_cb=status
        )
        print(f"agent (after refusal): {reply2}")
        return 0 if reply and reply2 and reply2 != reply else 1
    finally:
        await agent.stop()
        await wa.aclose()
        cal.close()


if __name__ == "__main__":
    if sys.platform == "win32":
        # psycopg async cannot run on the default ProactorEventLoop.
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    sys.exit(
        asyncio.run(
            _self_check(" ".join(sys.argv[1:]) or "hi, I need a meeting tomorrow afternoon")
        )
    )
