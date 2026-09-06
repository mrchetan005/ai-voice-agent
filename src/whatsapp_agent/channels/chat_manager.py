"""WhatsApp TEXT assistant: every inbound message goes through the same
checkpointed BookingAgent (channel='chat'). Threads are per sender and
shared with voice calls — the agent remembers people across channels.

Long-lived service semantics: unlike the old CLI mode this never
self-exits on idle; a background sweep flushes stale chat sessions
(>30 min silence) into voiceagent_sessions instead.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging

from whatsapp_agent.agent.brain import BookingAgent
from whatsapp_agent.agent.prompts import render_profile_block
from whatsapp_agent.capabilities.booking.cal_client import CalClient
from whatsapp_agent.capabilities.booking.service import (
    BookingService,
    create_booking_service,
)
from whatsapp_agent.channels.client import WhatsAppClient
from whatsapp_agent.channels.events import EventRouter
from whatsapp_agent.config import get_settings
from whatsapp_agent.infra.metering import ChatSessionTracker, record_session
from whatsapp_agent.infra.redis import RedisGateway
from whatsapp_agent.infra.runtime_config import RuntimeConfig
from whatsapp_agent.infra.stores import SessionStore

logger = logging.getLogger("whatsapp_agent")

_SWEEP_INTERVAL_S = 300.0


class ChatManager:
    def __init__(
        self,
        router: EventRouter,
        wa: WhatsAppClient,
        cal: CalClient,
        session_store: SessionStore,
        redis: RedisGateway | None = None,
        config: RuntimeConfig | None = None,
    ) -> None:
        self.router = router
        self.wa = wa
        self.cal = cal
        self.session_store = session_store
        self.redis = redis or RedisGateway(None)
        self.config = config or RuntimeConfig("")
        # One BookingAgent (+ its own DB conn) per sender; fine for the
        # 5-recipient test allowlist — pool connections before multi-tenant.
        self.agents: dict[str, BookingAgent] = {}
        self.services: dict[str, BookingService] = {}
        self.trackers: dict[str, ChatSessionTracker] = {}
        self._tasks: list[asyncio.Task] = []

    async def start(self) -> None:
        self._tasks = [
            asyncio.create_task(self._loop(), name="chat-loop"),
            asyncio.create_task(self._sweep(), name="chat-sweep"),
        ]
        logger.info("chat manager started")

    async def stop(self) -> None:
        for task in self._tasks:
            task.cancel()
        for task in self._tasks:
            with contextlib.suppress(asyncio.CancelledError):
                await task
        self._tasks = []
        for sender in list(self.trackers):
            await self._flush(sender)
        for agent in self.agents.values():
            await agent.stop()
        for service in self.services.values():
            await service.aclose()

    # -- internals ------------------------------------------------------------

    async def _flush(self, sender: str) -> None:
        """Write one chat-session row and reset per-session state."""
        tracker, service = self.trackers.get(sender), self.services.get(sender)
        if tracker is None or service is None or not tracker.turns:
            return
        await record_session(
            self.session_store, tracker.meter,
            turns=tracker.turns, service=service,
            telemetry=None, started_at=tracker.started_at,
        )
        service.session_actions.clear()
        service.session_errors.clear()

    async def _sweep(self) -> None:
        """Flush chat sessions that went silent (>30 min) without waiting
        for the sender's next message."""
        while True:
            await asyncio.sleep(_SWEEP_INTERVAL_S)
            for sender in list(self.trackers):
                tracker = self.trackers[sender]
                if tracker.turns and tracker.stale():
                    with contextlib.suppress(Exception):
                        await self._flush(sender)
                    tracker.reset()
                    if sender in self.services:
                        self.services[sender].meter = tracker.meter
                    if sender in self.agents:
                        self.agents[sender].meter = tracker.meter

    async def _ensure_sender(self, sender: str) -> None:
        if sender in self.agents:
            return
        settings = get_settings()
        # Chat passes NO waiter: the chat loop consumes button taps itself
        # and unlocks the email gate via mark_confirmed.
        service = await create_booking_service(
            self.cal, self.wa, None, sender,
            settings.cal_event_type_id, settings.cal_timezone, "whatsapp-chat",
            redis=self.redis,
        )
        self.services[sender] = service
        agent = BookingAgent(
            self.cal, settings.cal_event_type_id, self.wa, sender, service,
            timezone=settings.cal_timezone, business_name=settings.business_name,
            model=await self.config.resolve("scheduler_model"),
            channel="chat", profile_note=render_profile_block(service.profile),
            redis=self.redis,
        )
        await agent.start()
        self.agents[sender] = agent
        self.trackers[sender] = ChatSessionTracker(sender)

    async def _loop(self) -> None:
        pending_text: asyncio.Task | None = None
        pending_button: asyncio.Task | None = None
        try:
            while True:
                # Two inbound lanes: typed texts AND button taps (email
                # confirm). Persistent getter tasks so nothing is dropped.
                if pending_text is None:
                    pending_text = asyncio.create_task(self.router.chat_texts.get())
                if pending_button is None:
                    pending_button = asyncio.create_task(self.router.chat_buttons.get())
                done, _ = await asyncio.wait(
                    {pending_text, pending_button},
                    return_when=asyncio.FIRST_COMPLETED,
                )
                if pending_text in done:
                    msg = pending_text.result()
                    pending_text = None
                    sender, text = msg.from_number, msg.text.strip()
                else:
                    tap = pending_button.result()
                    pending_button = None
                    sender = tap.from_number
                    if tap.button_id.startswith("email_ok:") and sender in self.services:
                        # Unlock the booking gate BEFORE the model sees the tap.
                        self.services[sender].mark_confirmed(
                            tap.button_id.split(":", 1)[1]
                        )
                    text = f"[user tapped button: {tap.title}]"
                if not sender or not text:
                    continue
                logger.info("[%s] %s", sender, text[:80])
                await self._ensure_sender(sender)
                tracker = self.trackers[sender]
                if tracker.stale():
                    # >30 min of silence: close the old session, start fresh.
                    await self._flush(sender)
                    tracker.reset()
                self.services[sender].meter = tracker.meter
                self.agents[sender].meter = tracker.meter
                tracker.touch()
                tracker.turns.append(("user", text))
                try:
                    reply = await self.agents[sender].respond(
                        text, f"wa-{sender.lstrip('+')}"
                    )
                except Exception:
                    logger.exception("chat turn failed")
                    reply = ("Sorry, something went wrong on my side — "
                             "could you send that again?")
                if reply:
                    await self.wa.send_text(sender, reply)
                    tracker.turns.append(("assistant", reply))
                    tracker.meter.add("whatsapp", "messages", 1)
                    logger.info("[assistant -> %s] %s", sender, reply[:80])
        finally:
            for task in (pending_text, pending_button):
                if task is not None:
                    task.cancel()
