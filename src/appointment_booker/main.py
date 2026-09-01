"""Appointment booker on WhatsApp: voice calls (in/out) + text chat,
Gemini Live + Cal.com, transcripts in Neon.

Modes:
    uv run --env-file .env appointment-booker                     # outbound call
    uv run --env-file .env appointment-booker --inbound           # answer users' calls
    uv run --env-file .env appointment-booker --chat              # book via WA text
    uv run --env-file .env appointment-booker --serve-only        # webhook setup
    uv run --env-file .env appointment-booker --permission-only   # send call-permission ask
    --skip-permission   reuse a grant from the last 7 days
    --brain single|dual single: Gemini Live calls Cal.com tools itself (default,
                        lowest latency); dual: LangGraph agent behind send_to_agent

Requires a public HTTPS URL for --port (ngrok in dev) configured in the Meta
App dashboard with WHATSAPP_VERIFY_TOKEN, subscribed to `calls` + `messages`.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import datetime as dt
import json
import logging
import os
import re
import sys
import uuid

from appointment_booker.booking_handler import BookingCall
from appointment_booker.booking_service import (
    BookingService,
    create_booking_service,
)
from appointment_booker.cal_client import CalClient
from appointment_booker.graph import BookingAgent
from appointment_booker.metering import (
    ChatSessionTracker,
    UsageMeter,
    write_session_record,
)
from appointment_booker.native_tools import (
    TextBridge,
    TranscriptStore,
    build_native_tools,
)
from appointment_booker.prompts import (
    CALL_CONNECTED_NUDGE,
    CHAT_DURING_CALL_NUDGE,
    DUAL_BRAIN_VOICE_PROMPT,
    INBOUND_PICKUP_NUDGE,
    build_single_brain_prompt,
    render_profile_block,
)
from appointment_booker.recap import RecapSender
from appointment_booker.stores import SessionStore
from appointment_booker.transport_whatsapp import WhatsAppCallTransport
from appointment_booker.webhooks import WebhookHub
from appointment_booker.whatsapp_api import WhatsAppClient
from voiceagent import OrchestratorBridge, SessionConfig
from voiceagent.guardrails_and_eval import GuardrailPipeline, TelemetryRecorder
from voiceagent.providers import GeminiLiveProxy

logger = logging.getLogger("appointment_booker")


async def _signal_end(hub: WebhookHub) -> None:
    """Agent-initiated hangup: same teardown path as a remote hangup."""
    hub.call_ended.set()


async def _send_recap(
    wa: WhatsAppClient,
    recipient: str,
    business: str,
    turns: list[tuple[str, str]],
    service: BookingService | None,
    meter: UsageMeter | None = None,
) -> None:
    """Post-call recap on WhatsApp. Best-effort with a hard time cap —
    a failed or slow recap must never block teardown."""
    if not turns or service is None:
        return
    with contextlib.suppress(Exception):
        upcoming = await service.booking_store.list_upcoming(recipient)
        recap = RecapSender(wa, recipient, business, meter=meter)
        await asyncio.wait_for(
            recap.send(turns, service.session_actions, upcoming), timeout=30.0
        )


async def _record_session(
    session_store: SessionStore,
    meter: UsageMeter,
    *,
    turns: list[tuple[str, str]],
    service: BookingService | None,
    telemetry: object,
    started_at: dt.datetime,
    language: str = "",
) -> None:
    """Persist the voiceagent_sessions row at teardown (time-capped)."""
    with contextlib.suppress(Exception):
        await asyncio.wait_for(
            write_session_record(
                session_store, meter,
                turns=turns,
                actions=list(service.session_actions) if service else [],
                errors=list(service.session_errors) if service else [],
                telemetry=telemetry,
                started_at=started_at,
                db_degraded=session_store.degraded
                or (service.booking_store.degraded if service else False),
                language=language,
            ),
            timeout=15.0,
        )


async def _inject_chat_text(proxy: GeminiLiveProxy, text: str) -> None:
    """Mid-call WhatsApp text -> the live voice session reads it aloud."""
    await proxy._ws.send(json.dumps({
        "clientContent": {
            "turns": [{"role": "user", "parts": [
                {"text": CHAT_DURING_CALL_NUDGE.format(text=text)}
            ]}],
            "turnComplete": True,
        }
    }))


async def _single_brain_setup(
    cal: CalClient,
    wa: WhatsAppClient,
    hub: WebhookHub,
    caller: str,
    event_type_id: int,
    timezone: str,
    business: str,
    inbound: bool,
) -> tuple[str, dict, TranscriptStore, TextBridge, BookingService]:
    """SINGLE-BRAIN session pieces: Gemini Live is the whole agent — persona
    + availability snapshot in its system instruction, Cal.com/WhatsApp tools
    registered natively, transcripts persisted asynchronously, farewell
    backstop armed. Shared by outbound calls and the inbound answer loop."""
    from datetime import date, datetime, timedelta
    from zoneinfo import ZoneInfo

    today = datetime.now(ZoneInfo(timezone)).date()  # business-local, not server-local
    slots = await asyncio.to_thread(
        cal.get_slots, event_type_id,
        today + timedelta(days=1), today + timedelta(days=7),
        timezone,
    )
    # Weekday names inline: the model mislabeled dates ("Saturday, August
    # twenty eighth" for a Friday) when given bare ISO dates.
    snapshot = "; ".join(
        f"{date.fromisoformat(day).strftime('%A')} {day}: "
        f"{','.join(e['start'][11:16] for e in entries[:8])}"
        for day, entries in list(slots.items())[:7]
    )
    # Transcripts: Gemini's transcription events -> async Neon writes
    # (never blocks the audio path).
    transcript_store = TranscriptStore(
        f"wa-{caller.lstrip('+')}", os.environ["DATABASE_URL"]
    )
    await transcript_store.connect()

    # Booking service: profile + booking stores, email confirmation state,
    # and every Cal.com/WhatsApp booking operation (shared with dual-brain).
    service = await create_booking_service(
        cal, wa, hub, caller, event_type_id, timezone, "voice-single-brain"
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
    text_bridge = TextBridge(hub, transcript_store)
    service.email_waiter = text_bridge.wait_email

    provider_options: dict = {
        "native_tools": build_native_tools(
            service, end_call_cb=lambda: _signal_end(hub),
        )
    }

    # Farewell backstop: the model often says goodbye WITHOUT calling
    # end_call. If an assistant turn ends in a farewell, hang up ourselves
    # after a grace window; any further speech cancels the timer.
    farewell = re.compile(
        r"\b(good\s?bye|bye+|alvida|अलविदा|फिर मिलेंगे|take care)[\s.!।]*$",
        re.IGNORECASE,
    )
    pending_end: list[asyncio.Task] = []

    async def _delayed_hangup() -> None:
        await asyncio.sleep(6.0)
        logger.info("farewell backstop: hanging up")
        hub.call_ended.set()

    async def on_transcription(role: str, text: str) -> None:
        await transcript_store.save(role, text)
        for task in pending_end:
            task.cancel()
        pending_end.clear()
        if role == "assistant" and farewell.search(text.strip()):
            pending_end.append(asyncio.create_task(_delayed_hangup()))

    provider_options["on_transcription"] = on_transcription
    return system_prompt, provider_options, transcript_store, text_bridge, service


async def run_inbound(args: argparse.Namespace) -> int:
    """Answer loop: wait for users to CALL the business number, pick up,
    run a single-brain session, repeat. One call at a time (v1)."""
    hub = WebhookHub(port=args.port)
    await hub.start()
    wa = WhatsAppClient()
    cal = CalClient()
    event_type_id = int(os.environ["CAL_EVENT_TYPE_ID"])
    timezone = os.environ.get("CAL_TIMEZONE", "Asia/Kolkata")
    business = os.environ.get("BUSINESS_NAME", "our office")
    session_store = SessionStore(os.environ["DATABASE_URL"])
    await session_store.connect()
    try:
        try:
            await wa.enable_calling()
        except Exception as exc:
            logger.warning("enable_calling: %s", exc)
        print("inbound mode: waiting for calls — open the business chat on "
              "WhatsApp and tap the call button")
        while True:
            incoming = await hub.incoming_calls.get()
            caller = incoming.from_number or os.environ["WHATSAPP_RECIPIENT"]
            print(f"incoming call from {caller}")
            hub.call_ended.clear()

            transport = WhatsAppCallTransport(wa, hub, caller)
            transcript_store = None
            text_bridge = None
            service = None
            try:
                system_prompt, provider_options, transcript_store, text_bridge, service = (
                    await _single_brain_setup(
                        cal, wa, hub, caller, event_type_id, timezone,
                        business, inbound=True,
                    )
                )
                meter = UsageMeter(uuid.uuid4().hex, caller, "voice-inbound", "single")
                service.meter = meter
                await transport.answer_call(incoming.call_id, incoming.sdp)

                config = SessionConfig(
                    language=os.environ.get("VOICEAGENT_LANGUAGE", "en-IN"),
                    tone="warm",
                    system_prompt=system_prompt,
                    input_sample_rate=16_000,
                    output_sample_rate=24_000,
                    provider_options=provider_options,
                )
                proxy = GeminiLiveProxy(config, transport)
                telemetry = TelemetryRecorder(config.session_id)
                proxy.telemetry = telemetry
                text_bridge.attach(
                    lambda text, proxy=proxy: _inject_chat_text(proxy, text)
                )
                text_bridge.start()
                started_at = dt.datetime.now(dt.UTC)
                session = asyncio.create_task(proxy.run())
                await asyncio.sleep(1.0)  # audio path settles
                await proxy._ws.send(json.dumps({
                    "clientContent": {
                        "turns": [{"role": "user", "parts": [
                            {"text": INBOUND_PICKUP_NUDGE}
                        ]}],
                        "turnComplete": True,
                    }
                }))
                await session
                await _send_recap(
                    wa, caller, business, transcript_store.session_turns, service,
                    meter,
                )
                meter.merge_gemini_live(proxy.usage)
                await _record_session(
                    session_store, meter,
                    turns=transcript_store.session_turns, service=service,
                    telemetry=telemetry, started_at=started_at,
                    language=config.language,
                )
                print("call ended; waiting for the next one")
            except Exception:
                logger.exception("inbound call failed")
            finally:
                if text_bridge is not None:
                    text_bridge.stop()
                await transport.close()
                if service is not None:
                    await service.aclose()
                if transcript_store is not None:
                    await transcript_store.close()
    finally:
        await session_store.close()
        cal.close()
        await wa.aclose()
        await hub.stop()


async def run_chat(args: argparse.Namespace) -> int:
    """WhatsApp TEXT booking: every inbound message goes through the same
    checkpointed BookingAgent (channel='chat'). Threads are per sender and
    shared with voice calls — the agent remembers callers across channels."""
    hub = WebhookHub(port=args.port)
    await hub.start()
    wa = WhatsAppClient()
    cal = CalClient()
    event_type_id = int(os.environ["CAL_EVENT_TYPE_ID"])
    timezone = os.environ.get("CAL_TIMEZONE", "Asia/Kolkata")
    business = os.environ.get("BUSINESS_NAME", "our office")
    session_store = SessionStore(os.environ["DATABASE_URL"])
    await session_store.connect()
    # ponytail: one BookingAgent (+ its own DB conn) per sender; fine for the
    # 5-recipient test allowlist — pool connections if this goes multi-tenant.
    agents: dict[str, BookingAgent] = {}
    services: dict[str, BookingService] = {}
    trackers: dict[str, ChatSessionTracker] = {}
    pending_text: asyncio.Task | None = None
    pending_button: asyncio.Task | None = None

    async def flush_tracker(sender: str) -> None:
        """Write one chat-session row and reset per-session state."""
        tracker, service = trackers.get(sender), services.get(sender)
        if tracker is None or service is None or not tracker.turns:
            return
        await _record_session(
            session_store, tracker.meter,
            turns=tracker.turns, service=service,
            telemetry=None, started_at=tracker.started_at,
        )
        service.session_actions.clear()
        service.session_errors.clear()

    print("chat mode: waiting for WhatsApp messages…")
    try:
        while True:
            # Two inbound lanes: typed texts AND button taps (email confirm).
            # Persistent getter tasks so no queued item is ever dropped.
            if pending_text is None:
                pending_text = asyncio.create_task(hub.text_messages.get())
            if pending_button is None:
                pending_button = asyncio.create_task(hub.button_replies.get())
            done, _pending = await asyncio.wait(
                {pending_text, pending_button},
                timeout=3600, return_when=asyncio.FIRST_COMPLETED,
            )
            if not done:
                print("no messages for an hour; shutting down")
                return 0
            if pending_text in done:
                msg = pending_text.result()
                pending_text = None
                sender, text = msg.from_number, msg.text.strip()
            else:
                tap = pending_button.result()
                pending_button = None
                sender = tap.from_number
                if tap.button_id.startswith("email_ok:") and sender in services:
                    # Unlock the booking gate BEFORE the model sees the tap.
                    services[sender].mark_confirmed(tap.button_id.split(":", 1)[1])
                text = f"[user tapped button: {tap.title}]"
            if not sender or not text:
                continue
            print(f"[{sender}] {text[:80]}")
            if sender not in agents:
                service = await create_booking_service(
                    cal, wa, hub, sender, event_type_id, timezone, "whatsapp-chat"
                )
                services[sender] = service
                agent = BookingAgent(
                    cal, event_type_id, wa, hub, sender, service,
                    timezone=timezone, business_name=business, channel="chat",
                    profile_note=render_profile_block(service.profile),
                )
                await agent.start()
                agents[sender] = agent
                trackers[sender] = ChatSessionTracker(sender)
            tracker = trackers[sender]
            if tracker.stale():
                # >30 min of silence: close the old chat session, start fresh.
                await flush_tracker(sender)
                tracker.reset()
            services[sender].meter = tracker.meter
            agents[sender].meter = tracker.meter
            tracker.touch()
            tracker.turns.append(("user", text))
            try:
                reply = await agents[sender].respond(text, f"wa-{sender.lstrip('+')}")
            except Exception:
                logger.exception("chat turn failed")
                reply = "Sorry, something went wrong on my side — could you send that again?"
            if reply:
                await wa.send_text(sender, reply)
                tracker.turns.append(("assistant", reply))
                tracker.meter.add("whatsapp", "messages", 1)
                print(f"[priya -> {sender}] {reply[:80]}")
    finally:
        for task in (pending_text, pending_button):
            if task is not None:
                task.cancel()
        for sender in list(trackers):
            await flush_tracker(sender)
        for agent in agents.values():
            await agent.stop()
        for service in services.values():
            await service.aclose()
        await session_store.close()
        cal.close()
        await wa.aclose()
        await hub.stop()


async def run(args: argparse.Namespace) -> int:
    hub = WebhookHub(port=args.port)
    await hub.start()

    if args.serve_only:
        print(f"webhook listening on :{args.port}/webhook — expose with: ngrok http {args.port}")
        print("then set the URL + WHATSAPP_VERIFY_TOKEN in the Meta App dashboard")
        await asyncio.Future()

    recipient = os.environ["WHATSAPP_RECIPIENT"]  # required for call modes only

    wa = WhatsAppClient()
    cal: CalClient | None = None
    transport: WhatsAppCallTransport | None = None
    booking_agent: BookingAgent | None = None
    transcript_store: TranscriptStore | None = None
    text_bridge: TextBridge | None = None
    service: BookingService | None = None
    session_store: SessionStore | None = None
    try:
        try:
            await wa.enable_calling()
            logger.info("calling enabled on number %s", wa.phone_number_id)
        except Exception as exc:
            # Already enabled or no calling access yet — the /calls request
            # later gives the authoritative error.
            logger.warning("enable_calling: %s", exc)

        if not args.skip_permission:
            await wa.send_permission_request(
                recipient,
                "We'd like to call you on WhatsApp to schedule your appointment.",
            )
            print("permission request sent — waiting for the user to tap Accept…")
            accepted = await hub.wait_permission(timeout_s=300)
            print(f"permission: {'ACCEPTED' if accepted else 'REJECTED'}")
            if args.permission_only or not accepted:
                return 0 if accepted else 1

        cal = CalClient()
        event_type_id = int(os.environ["CAL_EVENT_TYPE_ID"])
        timezone = os.environ.get("CAL_TIMEZONE", "Asia/Kolkata")
        business = os.environ.get("BUSINESS_NAME", "our office")
        session_store = SessionStore(os.environ["DATABASE_URL"])
        await session_store.connect()
        meter = UsageMeter(uuid.uuid4().hex, recipient, "voice-outbound", args.brain)
        provider_options: dict = {}
        call: BookingCall | None = None

        if args.brain == "single":
            system_prompt, provider_options, transcript_store, text_bridge, service = (
                await _single_brain_setup(
                    cal, wa, hub, recipient, event_type_id, timezone, business,
                    inbound=False,
                )
            )
        else:
            # DUAL-BRAIN (kept as an option): LangGraph agent behind
            # send_to_agent; richer control, one extra LLM hop per reply.
            system_prompt = DUAL_BRAIN_VOICE_PROMPT
            service = await create_booking_service(
                cal, wa, hub, recipient, event_type_id, timezone,
                "whatsapp-voice-agent",
            )
            booking_agent = BookingAgent(
                cal, event_type_id, wa, hub, recipient, service,
                timezone=timezone, business_name=business,
                profile_note=render_profile_block(service.profile),
            )
            booking_agent.meter = meter
            await booking_agent.start()

        service.meter = meter
        transport = WhatsAppCallTransport(wa, hub, recipient)
        print(f"placing WhatsApp call… (brain={args.brain})")
        await transport.place_call()

        config = SessionConfig(
            language=os.environ.get("VOICEAGENT_LANGUAGE", "en-IN"),
            tone="warm",
            system_prompt=system_prompt,
            input_sample_rate=16_000,   # Gemini Live input
            output_sample_rate=24_000,  # Gemini Live output
            provider_options=provider_options,
        )
        proxy = GeminiLiveProxy(config, transport)
        telemetry = TelemetryRecorder(config.session_id)
        proxy.telemetry = telemetry

        if text_bridge is not None:
            text_bridge.attach(lambda text: _inject_chat_text(proxy, text))
            text_bridge.start()

        if args.brain == "dual":
            # thread_id = caller's number: dropped calls resume, repeat
            # callers remembered (state in Neon).
            call = BookingCall(booking_agent, thread_id=f"wa-{recipient.lstrip('+')}")
            bridge = OrchestratorBridge(
                proxy, call.handler, config,
                GuardrailPipeline("standard", config.language), telemetry,
            )
            proxy.bind(bridge)

        started_at = dt.datetime.now(dt.UTC)
        session = asyncio.create_task(proxy.run())
        # Greet only once the callee has actually picked up — speaking during
        # RINGING is how the caller misses the first sentence.
        try:
            await asyncio.wait_for(hub.call_accepted.wait(), timeout=90)
        except TimeoutError:
            print("call was never answered; hanging up")
            await proxy.stop()
            await session
            return 1
        await asyncio.sleep(0.7)  # let the audio path settle after pickup
        if call is not None:  # dual-brain: LangGraph writes the greeting
            await proxy.speak_text(await call.greet())
        else:  # single-brain: nudge the live model to open in persona
            await proxy._ws.send(json.dumps({
                "clientContent": {
                    "turns": [{"role": "user", "parts": [
                        {"text": CALL_CONNECTED_NUDGE}
                    ]}],
                    "turnComplete": True,
                }
            }))

        await session  # ends on hangup (webhook), goAway exhaustion, or error
        turns = (
            transcript_store.session_turns if transcript_store is not None
            else call.session_turns if call is not None
            else []
        )
        await _send_recap(wa, recipient, business, turns, service, meter)
        meter.merge_gemini_live(proxy.usage)
        await _record_session(
            session_store, meter,
            turns=turns, service=service,
            telemetry=telemetry, started_at=started_at,
            language=config.language,
        )
        print("\ncall ended. latency report:")
        print(json.dumps(telemetry.report(), indent=2))
        if call is not None:
            print(f"conversation thread: {call.thread_id} (state persisted in Postgres)")
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
        if session_store is not None:
            await session_store.close()
        if transcript_store is not None:
            await transcript_store.close()
        if cal is not None:
            cal.close()
        await wa.aclose()
        await hub.stop()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="WhatsApp appointment booking call")
    parser.add_argument("--port", type=int, default=8080, help="webhook port")
    parser.add_argument("--serve-only", action="store_true", help="just run the webhook server")
    parser.add_argument("--permission-only", action="store_true", help="send permission request and exit")
    parser.add_argument("--skip-permission", action="store_true", help="permission already granted")
    parser.add_argument(
        "--brain", choices=("single", "dual"), default="single",
        help="single: Gemini Live calls tools directly (lowest latency); "
             "dual: LangGraph agent behind send_to_agent (Neon-checkpointed)",
    )
    parser.add_argument(
        "--inbound", action="store_true",
        help="answer calls users make TO the business number (single-brain)",
    )
    parser.add_argument(
        "--chat", action="store_true",
        help="handle WhatsApp TEXT messages: converse and book in chat",
    )
    return parser.parse_args()


def cli() -> None:
    """Console entry point (`appointment-booker` after install)."""
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    if sys.platform == "win32":
        # psycopg async (Neon checkpointer) cannot run on ProactorEventLoop.
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    try:
        cli_args = parse_args()
        if cli_args.chat:
            entry = run_chat(cli_args)
        elif cli_args.inbound:
            entry = run_inbound(cli_args)
        else:
            entry = run(cli_args)
        sys.exit(asyncio.run(entry))
    except KeyboardInterrupt:
        sys.exit(130)


if __name__ == "__main__":
    cli()
