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
    uv run --env-file .env python -m whatsapp_agent.agent.brain "hi, I'd like a meeting tomorrow"
"""

from __future__ import annotations

import asyncio
import contextlib
import datetime as dt
import json
import logging
import os
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

from whatsapp_agent.agent.prompts import (
    AVAILABILITY_GUIDE,
    CHAT_EMAIL_NOTE,
    CHAT_RULES,
    VOICE_DELIVERY_NOTE,
    VOICE_RULES,
)
from whatsapp_agent.capabilities.booking.cal_client import CalClient
from whatsapp_agent.capabilities.booking.service import BookingService
from whatsapp_agent.channels.client import WhatsAppClient

logger = logging.getLogger("whatsapp_agent")

# Brain LLM gateways (all OpenAI-compatible except gemini). BYOK gateways
# (OpenRouter key vault, a self-hosted LiteLLM proxy) hold the provider
# keys on THEIR side — here they are just a base_url + one api key.
_BRAIN_PRESETS: dict[str, tuple[str | None, str]] = {
    "groq": ("https://api.groq.com/openai/v1", "GROQ_API_KEY"),
    "openai": (None, "OPENAI_API_KEY"),
    "openrouter": ("https://openrouter.ai/api/v1", "OPENROUTER_API_KEY"),
}


def make_brain_llm(
    model_spec: str | None = None, temperature: float = 0.6
) -> tuple[Any, str]:
    """(llm, cost_provider_label) from a '<provider>/<model>' spec.

    Providers: gemini (default; bare '<model>' means gemini) | groq |
    openai | openrouter | custom. 'custom' reads SCHEDULER_BASE_URL and
    SCHEDULER_API_KEY_ENV (default CUSTOM_LLM_API_KEY) — any
    OpenAI-compatible gateway, e.g. a LiteLLM proxy. The model must
    support tool calling (the booking brain is a tool agent). Examples:
        SCHEDULER_MODEL=gemini-3.6-flash
        SCHEDULER_MODEL=groq/openai/gpt-oss-20b
        SCHEDULER_MODEL=openrouter/anthropic/claude-sonnet-5
        SCHEDULER_MODEL=custom/my-litellm-alias
    """
    from whatsapp_agent.config import get_settings

    settings = get_settings()
    spec = model_spec or settings.scheduler_model
    provider, sep, model = spec.partition("/")
    if not sep or provider not in (*_BRAIN_PRESETS, "gemini", "custom"):
        provider, model = "gemini", spec
    if provider == "gemini":
        return ChatGoogleGenerativeAI(model=model, temperature=temperature), "gemini_flash"

    from langchain_openai import ChatOpenAI

    if provider == "custom":
        base_url = settings.scheduler_base_url
        if not base_url:
            raise RuntimeError("SCHEDULER_MODEL=custom/... needs SCHEDULER_BASE_URL")
        key_env = settings.scheduler_api_key_env
        label = "custom_llm"
    else:
        base_url, key_env = _BRAIN_PRESETS[provider]
        label = provider
    api_key = os.environ.get(key_env)
    if not api_key:
        raise RuntimeError(f"brain LLM {spec!r} needs {key_env} set")
    return ChatOpenAI(
        model=model, temperature=temperature, base_url=base_url, api_key=api_key
    ), label


class BookingAgent:
    """Checkpointed conversational agent; one instance per process,
    one thread_id per caller."""

    def __init__(
        self,
        cal: CalClient,
        event_type_id: int,
        wa: WhatsAppClient,
        recipient: str,
        service: BookingService,
        timezone: str = "Asia/Kolkata",
        db_url: str | None = None,
        model: str | None = None,
        business_name: str = "our office",
        channel: str = "voice",  # "voice" (call relay) or "chat" (WA text)
        profile_note: str = "",
    ) -> None:
        self._channel = channel
        self.service = service
        # Optional UsageMeter: token usage from every model invocation.
        self.meter: Any = None
        self._cal = cal
        self._event_type_id = event_type_id
        self._tz = ZoneInfo(timezone)
        self._tz_name = timezone
        from whatsapp_agent.config import get_settings

        self._db_url = db_url or get_settings().database_url
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
            start_local_iso: str,
            attendee_name: str,
            topic: str,
            email: str,
            book_anyway: bool = False,
        ) -> str:
            """Book the confirmed slot. start_local_iso must be the exact
            slot start copied from get_available_slots output. Call ONLY
            after the caller clearly said yes to this specific time AND
            their email is confirmed. Returns EMAIL_REQUIRED /
            EMAIL_NOT_CONFIRMED / EXISTING_BOOKING when preconditions are
            missing; set book_anyway=true only after the caller chose to
            keep an existing booking and add this one."""
            return json.dumps(self._run_on_loop(
                service.book(start_local_iso, attendee_name, topic, email,
                             book_anyway=book_anyway)
            ))

        @tool
        def request_email_over_whatsapp(wait_seconds: int = 45) -> str:
            """Send the caller a WhatsApp text asking for their email and
            wait for the reply. Use while telling the caller you've sent it.
            Email is REQUIRED before booking. Returns the email, or NO_REPLY
            if none arrives in time (you may call again to keep waiting)."""
            wait_s = float(min(max(wait_seconds, 10), 50))
            return json.dumps(self._run_on_loop(service.request_email(wait_s)))

        @tool
        def confirm_email_on_whatsapp(email: str) -> str:
            """Send the collected email back on WhatsApp with Confirm/Edit
            buttons. Booking is blocked until the caller confirms. On voice
            calls this waits for the tap (call again on NO_REPLY); in chat
            the tap arrives as the caller's next message — do NOT book until
            you see they confirmed."""
            sent = self._run_on_loop(service.send_email_confirmation(email))
            if sent.get("status") != "CONFIRMATION_SENT":
                return json.dumps(sent)
            if self._channel == "chat":
                return json.dumps({
                    "status": "CONFIRMATION_SENT",
                    "hint": "wait for the caller's Confirm tap before booking",
                })
            return json.dumps(self._run_on_loop(service.wait_email_confirmation(40.0)))

        @tool
        def list_my_bookings() -> str:
            """List the caller's upcoming appointments (uid, local time,
            topic). Call this FIRST when they ask to change, cancel or
            check a booking."""
            return json.dumps(self._run_on_loop(service.list_bookings()))

        @tool
        def cancel_appointment(booking_uid: str, reason: str = "") -> str:
            """Cancel a booking by uid. Read the booking back and get an
            explicit yes BEFORE calling."""
            return json.dumps(self._run_on_loop(service.cancel(booking_uid, reason)))

        @tool
        def reschedule_appointment(booking_uid: str, new_start_local_iso: str) -> str:
            """Move a booking to a new local time. Needs an explicit yes to
            the new exact slot first. Returns the NEW booking uid."""
            return json.dumps(self._run_on_loop(
                service.reschedule(booking_uid, new_start_local_iso)
            ))

        self._llm, self._llm_cost_provider = make_brain_llm(model)
        manage_tools = [
            confirm_email_on_whatsapp, list_my_bookings,
            cancel_appointment, reschedule_appointment,
        ]
        if channel == "chat":
            # Chat: user can just type their email — the wait-for-reply tool
            # would fight the chat loop for the same message queue.
            self._tools = [get_available_slots, book_appointment, *manage_tools]
            self._prompt = (
                CHAT_RULES.format(business_name=business_name)
                + AVAILABILITY_GUIDE
                + CHAT_EMAIL_NOTE
            )
        else:
            self._tools = [
                get_available_slots, book_appointment,
                request_email_over_whatsapp, *manage_tools,
            ]
            self._prompt = (
                VOICE_RULES.format(business_name=business_name)
                + AVAILABILITY_GUIDE
                + VOICE_DELIVERY_NOTE
            )
        if profile_note:
            self._prompt += profile_note

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
                            output = event.get("data", {}).get("output")
                            # One usage record per model invocation (tool
                            # decisions + final reply) — summing is correct.
                            if self.meter is not None and (
                                usage := getattr(output, "usage_metadata", None)
                            ):
                                self.meter.add(self._llm_cost_provider, "input_tokens",
                                               usage.get("input_tokens", 0))
                                self.meter.add(self._llm_cost_provider, "output_tokens",
                                               usage.get("output_tokens", 0))
                            # Gemini 3.x content is a list of parts, not a
                            # plain string; the LAST model turn is the reply
                            # (earlier ones are tool-call decisions).
                            reply = self._extract_text(output)
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
    from whatsapp_agent.capabilities.booking.service import create_booking_service
    from whatsapp_agent.config import get_settings

    logging.basicConfig(level=logging.INFO)
    settings = get_settings()
    cal = CalClient()
    wa = WhatsAppClient()
    recipient = settings.whatsapp_recipient
    event_type_id = settings.cal_event_type_id
    timezone = settings.cal_timezone
    service = await create_booking_service(
        cal, wa, None, recipient, event_type_id, timezone, "self-check"
    )
    agent = BookingAgent(
        cal, event_type_id, wa, recipient, service, timezone=timezone,
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
        await service.aclose()
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
