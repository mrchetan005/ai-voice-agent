"""Voice-call orchestration: ONE runner for inbound and outbound calls.

Provider policy lives here: gemini-live is voice+brain (single) or
voice-only (dual); openai-realtime and split are strict voice front-ends
with no native booking tools, so they always run dual-brain behind the
LangGraph agent. v1 serializes calls with one lock — going concurrent
later means removing the lock, not rearchitecting (sessions are already
per-call).
"""

from __future__ import annotations

import asyncio
import contextlib
import datetime as dt
import json
import logging
import re
import uuid
from dataclasses import dataclass

from voiceagent import OrchestratorBridge, SessionConfig
from voiceagent.guardrails_and_eval import GuardrailPipeline, TelemetryRecorder
from voiceagent.providers import (
    GeminiLiveProxy,
    OpenAIRealtimeProxy,
    SplitStackProxy,
)
from whatsapp_agent.agent.brain import BookingAgent
from whatsapp_agent.agent.call_handler import BookingCall
from whatsapp_agent.agent.prompts import (
    CALL_CONNECTED_NUDGE,
    CHAT_DURING_CALL_NUDGE,
    DUAL_BRAIN_VOICE_PROMPT,
    INBOUND_PICKUP_NUDGE,
    build_single_brain_prompt,
    render_profile_block,
)
from whatsapp_agent.agent.recap import RecapSender
from whatsapp_agent.capabilities.booking.cal_client import CalClient, get_slots_cached
from whatsapp_agent.capabilities.booking.service import (
    BookingService,
    create_booking_service,
)
from whatsapp_agent.capabilities.booking.voice_tools import build_native_tools
from whatsapp_agent.channels.client import WhatsAppClient
from whatsapp_agent.channels.events import (
    CallEvent,
    CallSession,
    EventRouter,
    TextBridge,
)
from whatsapp_agent.channels.transport import WhatsAppCallTransport
from whatsapp_agent.config import get_settings
from whatsapp_agent.infra.metering import UsageMeter, record_session
from whatsapp_agent.infra.redis import RedisGateway
from whatsapp_agent.infra.runtime_config import RuntimeConfig
from whatsapp_agent.infra.stores import SessionStore, TranscriptStore

logger = logging.getLogger("whatsapp_agent")

PROVIDERS = ("gemini-live", "openai-realtime", "split")

# (input_sample_rate, output_sample_rate); OpenAI Realtime's wire is 24 kHz
# both ways — the WhatsApp transport resamples either geometry to Opus 48 k.
_PROVIDER_RATES: dict[str, tuple[int, int]] = {
    "gemini-live": (16_000, 24_000),
    "openai-realtime": (24_000, 24_000),
    "split": (16_000, 24_000),
}

# Friendly Cartesia voice aliases; anything else passes through verbatim
# (Gemini prebuilt names like Despina, OpenAI voices like marin, raw ids).
VOICE_ALIASES = {
    "rohan": "4877b818-c7fe-4c89-b1cf-eadf8e23da72",
    "kavita": "56e35e2d-6eb6-4226-ab8b-9776515a7094",
}

_FAREWELL_RE = re.compile(
    r"\b(good\s?bye|bye+|alvida|अलविदा|फिर मिलेंगे|take care)[\s.!।]*$",
    re.IGNORECASE,
)


def _resolve_voice(arg: str | None) -> str | None:
    voice = arg or get_settings().voiceagent_voice_id or None
    return VOICE_ALIASES.get(voice.lower(), voice) if voice else None


def _resolve_brain(provider: str, requested: str) -> str:
    if provider != "gemini-live" and requested == "single":
        logger.info("%s has no native booking tools — using dual brain", provider)
        return "dual"
    return requested


async def _split_provider_options(config: RuntimeConfig) -> dict:
    """Split-stack knobs, resolved runtime-config-first (env fallback).
    ASR defaults to Deepgram nova-3 MULTILINGUAL — callers here code-switch
    between English and Indian languages mid-sentence."""
    opts: dict = {
        "asr": await config.resolve("split_asr"),
        "tts": await config.resolve("split_tts"),
        "asr_language": await config.resolve("split_asr_language"),
    }
    if asr_model := await config.resolve("split_asr_model"):
        opts["asr_model"] = asr_model
    if tts_model := await config.resolve("split_tts_model"):
        opts["tts_model"] = tts_model
    return opts


def _build_proxy(provider: str, config: SessionConfig, transport):
    if provider == "openai-realtime":
        return OpenAIRealtimeProxy(config, transport)
    if provider == "split":
        return SplitStackProxy(config, transport)
    return GeminiLiveProxy(config, transport)


@dataclass
class CallSpec:
    """Everything one call run needs, resolved before dialing."""

    peer: str
    direction: str  # "inbound" | "outbound"
    provider: str = "gemini-live"
    brain: str = "single"
    voice: str | None = None
    incoming: CallEvent | None = None  # inbound SDP offer
    call_ref: str = ""


class CallBusy(Exception):
    """Another call is active (per-phone or global serialization)."""


async def _single_brain_setup(
    cal: CalClient,
    wa: WhatsAppClient,
    session: CallSession,
    caller: str,
    event_type_id: int,
    timezone: str,
    business: str,
    inbound: bool,
    redis: RedisGateway | None = None,
) -> tuple[str, dict, TranscriptStore, TextBridge, BookingService]:
    """SINGLE-BRAIN session pieces: Gemini Live is the whole agent — persona
    + availability snapshot in its system instruction, Cal.com/WhatsApp tools
    registered natively, transcripts persisted asynchronously, farewell
    backstop armed."""
    from datetime import date, datetime, timedelta
    from zoneinfo import ZoneInfo

    today = datetime.now(ZoneInfo(timezone)).date()  # business-local, not server-local
    slots = await get_slots_cached(
        cal, event_type_id,
        today + timedelta(days=1), today + timedelta(days=7),
        timezone, redis=redis,
    )
    # Weekday names inline: the model mislabeled dates ("Saturday, August
    # twenty eighth" for a Friday) when given bare ISO dates.
    snapshot = "; ".join(
        f"{date.fromisoformat(day).strftime('%A')} {day}: "
        f"{','.join(e['start'][11:16] for e in entries[:8])}"
        for day, entries in list(slots.items())[:7]
    )
    transcript_store = TranscriptStore(
        f"wa-{caller.lstrip('+')}", get_settings().database_url
    )
    await transcript_store.connect()

    service = await create_booking_service(
        cal, wa, session, caller, event_type_id, timezone, "voice-single-brain",
        redis=redis,
    )

    # Cross-channel context: previous calls AND chats with this number.
    history = await transcript_store.load_recent(30)
    system_prompt = build_single_brain_prompt(
        business_name=business,
        timezone=timezone,
        snapshot=snapshot,
        inbound=inbound,
        history="\n".join(f"{role}: {content[:150]}" for role, content in history),
        profile=render_profile_block(service.profile),
    )

    # Chat<->call sync: texts sent DURING the call are injected into the
    # live session and satisfy email waits (single queue consumer).
    text_bridge = TextBridge(session, transcript_store)
    service.email_waiter = text_bridge.wait_email

    async def _end_call() -> None:
        session.ended.set()

    provider_options: dict = {
        "native_tools": build_native_tools(service, end_call_cb=_end_call)
    }

    # Farewell backstop: the model often says goodbye WITHOUT calling
    # end_call. If an assistant turn ends in a farewell, hang up ourselves
    # after a grace window; any further speech cancels the timer.
    pending_end: list[asyncio.Task] = []

    async def _delayed_hangup() -> None:
        await asyncio.sleep(6.0)
        logger.info("farewell backstop: hanging up")
        session.ended.set()

    async def on_transcription(role: str, text: str) -> None:
        await transcript_store.save(role, text)
        for task in pending_end:
            task.cancel()
        pending_end.clear()
        if role == "assistant" and _FAREWELL_RE.search(text.strip()):
            pending_end.append(asyncio.create_task(_delayed_hangup()))

    provider_options["on_transcription"] = on_transcription
    return system_prompt, provider_options, transcript_store, text_bridge, service


async def _send_recap(
    wa: WhatsAppClient,
    recipient: str,
    business: str,
    turns: list[tuple[str, str]],
    service: BookingService | None,
    meter: UsageMeter | None = None,
    enabled: bool = True,
) -> None:
    """Post-call recap on WhatsApp. Best-effort with a hard time cap —
    a failed or slow recap must never block teardown."""
    if not turns or service is None or not enabled:
        return
    with contextlib.suppress(Exception):
        upcoming = await service.booking_store.list_upcoming(recipient)
        recap = RecapSender(wa, recipient, business, meter=meter)
        await asyncio.wait_for(
            recap.send(turns, service.session_actions, upcoming), timeout=30.0
        )


class CallManager:
    """Owns the inbound answer loop and outbound call runs."""

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
        # Disabled gateway when Redis is off — every op fail-open, no None checks.
        self.redis = redis or RedisGateway(None)
        # Unconnected store resolves straight to Settings — same trick.
        self.config = config or RuntimeConfig("")
        # v1 policy: one active call at a time (see module docstring).
        self._call_lock = asyncio.Lock()
        self._inbound_task: asyncio.Task | None = None
        # API-started calls run as background tasks; the pending flag closes
        # the gap between start_outbound returning and the task taking the lock.
        self._outbound_pending = False
        self._bg_tasks: set[asyncio.Task] = set()

    @property
    def call_active(self) -> bool:
        return self._call_lock.locked() or self._outbound_pending

    # -- lifecycle -----------------------------------------------------------

    async def start(self) -> None:
        """Start answering inbound calls (idempotent)."""
        try:
            await self.wa.enable_calling()
        except Exception as exc:
            logger.warning("enable_calling: %s", exc)
        if self._inbound_task is None:
            self._inbound_task = asyncio.create_task(self._inbound_loop())
            logger.info("inbound answer loop started")

    async def stop(self) -> None:
        if self._inbound_task is not None:
            self._inbound_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._inbound_task
            self._inbound_task = None
        if self._bg_tasks:
            # Let an active API call finish; the lifespan's shutdown-grace
            # timeout bounds this wait (and cancels the calls on expiry).
            await asyncio.gather(*self._bg_tasks, return_exceptions=True)

    # -- inbound ---------------------------------------------------------------

    async def _inbound_loop(self) -> None:
        """Wait for users to CALL the business number, pick up, run, repeat.
        Inbound stays gemini-live single-brain (native tools)."""
        while True:
            incoming = await self.router.incoming_calls.get()
            caller = incoming.from_number or get_settings().whatsapp_recipient
            logger.info("incoming call from %s", caller)
            async with self._call_lock:
                session = self.router.open_call(caller, "inbound")
                spec = CallSpec(peer=caller, direction="inbound", incoming=incoming)
                try:
                    await self._run_call(spec, session)
                except Exception:
                    logger.exception("inbound call failed")
                finally:
                    self.router.close_call(session)
            logger.info("call ended; waiting for the next one")

    # -- outbound ---------------------------------------------------------------

    def start_outbound(
        self,
        *,
        peer: str | None = None,
        provider: str | None = None,
        brain: str | None = None,
        voice: str | None = None,
        skip_permission: bool = False,
    ) -> str:
        """API entry (POST /calls): begin the call in the background and
        return its ref immediately. Raises CallBusy when one is active."""
        if self.call_active:
            raise CallBusy(peer or "")
        call_ref = uuid.uuid4().hex[:12]
        self._outbound_pending = True

        async def _run() -> None:
            try:
                await self.run_outbound(
                    peer=peer, provider=provider, brain=brain, voice=voice,
                    skip_permission=skip_permission, call_ref=call_ref,
                )
            except CallBusy:
                logger.warning("call %s dropped: another call won the race", call_ref)
            except Exception:
                logger.exception("API call %s failed", call_ref)
            finally:
                self._outbound_pending = False

        task = asyncio.create_task(_run(), name=f"call-{call_ref}")
        self._bg_tasks.add(task)
        task.add_done_callback(self._bg_tasks.discard)
        return call_ref

    async def run_outbound(
        self,
        *,
        peer: str | None = None,
        provider: str | None = None,
        brain: str | None = None,
        voice: str | None = None,
        skip_permission: bool = False,
        permission_only: bool = False,
        call_ref: str = "",
    ) -> int:
        """Permission flow + one outbound call run. Raises CallBusy if a
        call is already active. None engine fields resolve through runtime
        config, then Settings."""
        settings = get_settings()
        peer = (peer or settings.whatsapp_recipient).lstrip("+")
        provider = await self.config.resolve("provider", provider)
        brain = await self.config.resolve("brain", brain)
        voice = await self.config.resolve("voice", voice)
        if self._call_lock.locked():
            raise CallBusy(peer)
        async with self._call_lock:
            # Cross-restart guard: a lingering call:active lock (e.g. API
            # retry racing a live call) refuses the dial; TTL is the backstop
            # if a crash ever skips release. Inbound pickups skip this — we
            # never refuse a user who is calling us.
            lock_key = f"call:active:{peer}"
            lock_token = call_ref or uuid.uuid4().hex
            if not await self.redis.acquire_lock(lock_key, lock_token, ttl_s=7200):
                raise CallBusy(peer)
            session = self.router.open_call(peer, "outbound")
            session.call_ref = call_ref
            try:
                try:
                    await self.wa.enable_calling()
                except Exception as exc:
                    logger.warning("enable_calling: %s", exc)
                if not skip_permission:
                    await self.wa.send_permission_request(
                        peer,
                        "We'd like to call you on WhatsApp to schedule your appointment.",
                    )
                    logger.info("permission request sent — waiting for Accept…")
                    try:
                        accepted = await session.wait_permission(timeout_s=300)
                    except TimeoutError:
                        logger.warning("permission request timed out")
                        return 1
                    logger.info("permission: %s", "ACCEPTED" if accepted else "REJECTED")
                    if permission_only or not accepted:
                        return 0 if accepted else 1
                spec = CallSpec(
                    peer=peer, direction="outbound", provider=provider,
                    brain=brain, voice=voice, call_ref=call_ref,
                )
                return await self._run_call(spec, session)
            finally:
                self.router.close_call(session)
                await self.redis.release_lock(lock_key, lock_token)

    # -- the one runner ------------------------------------------------------------

    async def _run_call(self, spec: CallSpec, session: CallSession) -> int:
        settings = get_settings()
        event_type_id = settings.cal_event_type_id
        timezone = settings.cal_timezone
        business = settings.business_name
        brain = _resolve_brain(spec.provider, spec.brain)
        input_rate, output_rate = _PROVIDER_RATES[spec.provider]
        meter = UsageMeter(
            uuid.uuid4().hex, spec.peer, f"voice-{spec.direction}", brain
        )
        provider_options: dict = {}
        call: BookingCall | None = None
        booking_agent: BookingAgent | None = None
        transport: WhatsAppCallTransport | None = None
        transcript_store: TranscriptStore | None = None
        text_bridge: TextBridge | None = None
        service: BookingService | None = None
        try:
            if brain == "single":
                (system_prompt, provider_options, transcript_store,
                 text_bridge, service) = await _single_brain_setup(
                    self.cal, self.wa, session, spec.peer, event_type_id,
                    timezone, business, inbound=(spec.direction == "inbound"),
                    redis=self.redis,
                )
            else:
                # DUAL-BRAIN: the voice engine relays; LangGraph is the brain.
                system_prompt = DUAL_BRAIN_VOICE_PROMPT
                service = await create_booking_service(
                    self.cal, self.wa, session, spec.peer, event_type_id,
                    timezone, "whatsapp-voice-agent", redis=self.redis,
                )
                booking_agent = BookingAgent(
                    self.cal, event_type_id, self.wa, spec.peer, service,
                    timezone=timezone, business_name=business,
                    model=await self.config.resolve("scheduler_model"),
                    profile_note=render_profile_block(service.profile),
                    redis=self.redis,
                )
                booking_agent.meter = meter
                await booking_agent.start()
            service.meter = meter

            transport = WhatsAppCallTransport(
                self.wa, session, spec.peer,
                input_sample_rate=input_rate, output_sample_rate=output_rate,
            )
            if spec.direction == "inbound":
                assert spec.incoming is not None
                await transport.answer_call(spec.incoming.call_id, spec.incoming.sdp)
            else:
                logger.info("placing WhatsApp call… (provider=%s, brain=%s)",
                            spec.provider, brain)
                await transport.place_call()

            if spec.provider == "split":
                provider_options.update(await _split_provider_options(self.config))
            config = SessionConfig(
                language=settings.voiceagent_language,
                tone="warm",
                system_prompt=system_prompt,
                voice_id=_resolve_voice(spec.voice),
                input_sample_rate=input_rate,
                output_sample_rate=output_rate,
                provider_options=provider_options,
            )
            proxy = _build_proxy(spec.provider, config, transport)
            telemetry = TelemetryRecorder(config.session_id)
            proxy.telemetry = telemetry

            if text_bridge is not None:  # gemini single-brain only
                text_bridge.attach(
                    lambda text: proxy.inject_text(CHAT_DURING_CALL_NUDGE.format(text=text))
                )
                text_bridge.start()

            if brain == "dual":
                # thread_id = caller's number: dropped calls resume, repeat
                # callers remembered (state in Neon).
                call = BookingCall(booking_agent, thread_id=f"wa-{spec.peer.lstrip('+')}")
                bridge = OrchestratorBridge(
                    proxy, call.handler, config,
                    GuardrailPipeline("standard", config.language), telemetry,
                )
                proxy.bind(bridge)

            started_at = dt.datetime.now(dt.UTC)
            session_task = asyncio.create_task(proxy.run())
            if spec.direction == "outbound":
                # Greet only once the callee has actually picked up — speaking
                # during RINGING is how the caller misses the first sentence.
                try:
                    await asyncio.wait_for(session.accepted.wait(), timeout=90)
                except TimeoutError:
                    logger.warning("call was never answered; hanging up")
                    await proxy.stop()
                    await session_task
                    return 1
                await asyncio.sleep(0.7)  # audio path settles after pickup
            else:
                await asyncio.sleep(1.0)  # audio path settles

            if call is not None:  # dual-brain: LangGraph writes the greeting
                await proxy.speak_text(await call.greet())
            elif spec.direction == "inbound":
                await proxy.inject_text(INBOUND_PICKUP_NUDGE)
            else:
                await proxy.inject_text(CALL_CONNECTED_NUDGE)

            await session_task  # ends on hangup, goAway exhaustion, or error

            turns = (
                transcript_store.session_turns if transcript_store is not None
                else call.session_turns if call is not None
                else []
            )
            await _send_recap(
                self.wa, spec.peer, business, turns, service, meter,
                enabled=await self.config.resolve("recap_enabled"),
            )
            if spec.provider == "openai-realtime":
                meter.merge_openai_realtime(proxy.usage)
            elif spec.provider == "split":
                meter.merge_split_stack(proxy.usage)
            else:
                meter.merge_gemini_live(proxy.usage)
            await record_session(
                self.session_store, meter,
                turns=turns, service=service,
                telemetry=telemetry, started_at=started_at,
                language=config.language,
            )
            logger.info("call ended. latency report:\n%s",
                        json.dumps(telemetry.report(), indent=2))
            return 0
        finally:
            if text_bridge is not None:
                text_bridge.stop()
            if transport is not None:
                await transport.close()
            if booking_agent is not None:
                await booking_agent.stop()
            if service is not None:
                await service.aclose()
            if transcript_store is not None:
                await transcript_store.close()
