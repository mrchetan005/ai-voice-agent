"""Standalone `voiceagent-whatsapp` service: Meta webhook + call bridge.

Runs as its own process (own webhook URL, own media lifecycle) so WhatsApp
scales independently of the worker fleet. It never runs an AgentSession — it
dispatches an agent into a LiveKit room and bridges the Meta WebRTC leg to
that room (see voiceagent.livekit.room_audio).

Endpoints:
  GET  /webhooks/whatsapp   Meta verification handshake
  POST /webhooks/whatsapp   Meta events (signature-checked)
  POST /v1/whatsapp/calls   place an outbound call  (bearer: PLATFORM_API_TOKEN)
  GET  /healthz

Live Meta verification is deferred; the SDP munge, webhook parsing, and
outbound-flow wiring are exercised offline (see tests/unit/test_whatsapp_*).
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from contextlib import asynccontextmanager
from typing import Any

from fastapi import Depends, FastAPI, HTTPException, Request, Response
from pydantic import BaseModel, Field

from voiceagent.agent import load_agents
from voiceagent.channels.whatsapp.meta_client import WhatsAppClient
from voiceagent.channels.whatsapp.sdp import munge_sdp_for_meta
from voiceagent.channels.whatsapp.webhook import (
    CallEvent,
    CallSession,
    EventRouter,
    SessionConflict,
    verify_signature,
    verify_subscription,
)
from voiceagent.livekit.api import delete_room, dispatch_agent, mint_join_token
from voiceagent.livekit.room_audio import RoomCallBridge
from voiceagent.metadata import SessionMetadata
from voiceagent.platform.auth import require_token
from voiceagent.settings import Settings, load_settings

logger = logging.getLogger("voiceagent")


class OutboundCallIn(BaseModel):
    agent_id: str
    to_number: str
    user_id: str | None = None
    prompt_vars: dict[str, str] = Field(default_factory=dict)
    request_permission: bool = False  # send a call-permission prompt first
    answer_timeout_s: float = 30.0


def _extract_call_id(response: dict[str, Any]) -> str:
    return (response.get("calls") or [{}])[0].get("id") or response.get("call_id") or ""


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or load_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.settings = settings
        app.state.agents = {a.name: a for a in load_agents(settings)}
        app.state.router = EventRouter()
        app.state.wa = WhatsAppClient(settings.whatsapp)
        app.state.bridges: dict[str, RoomCallBridge] = {}
        app.state.watchers: set[asyncio.Task[None]] = set()
        app.state.inbound_task = asyncio.create_task(_drain_inbound(app))
        logger.info("whatsapp service serving agents: %s", sorted(app.state.agents))
        yield
        app.state.inbound_task.cancel()
        for bridge in list(app.state.bridges.values()):
            await bridge.close()
        await app.state.wa.aclose()

    app = FastAPI(title="voiceagent whatsapp", docs_url=None, redoc_url=None, lifespan=lifespan)

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/webhooks/whatsapp")
    async def verify(request: Request) -> Response:
        q = request.query_params
        if verify_subscription(
            q.get("hub.mode", ""),
            q.get("hub.verify_token", ""),
            settings.whatsapp.verify_token,
        ):
            return Response(content=q.get("hub.challenge", ""), media_type="text/plain")
        raise HTTPException(403, "verification failed")

    @app.post("/webhooks/whatsapp")
    async def receive(request: Request) -> dict[str, str]:
        body = await request.body()
        if not verify_signature(
            body, request.headers.get("X-Hub-Signature-256"), settings.whatsapp.app_secret
        ):
            raise HTTPException(401, "bad signature")
        request.app.state.router.dispatch(await request.json())
        return {"status": "received"}

    @app.post("/v1/whatsapp/calls", status_code=201, dependencies=[Depends(require_token)])
    async def outbound(body: OutboundCallIn, request: Request) -> dict[str, str]:
        state = request.app.state
        if body.agent_id not in state.agents:
            raise HTTPException(404, f"unknown agent {body.agent_id!r}")
        return await _place_outbound(state, body)

    return app


async def _place_outbound(state: Any, body: OutboundCallIn) -> dict[str, str]:
    settings: Settings = state.settings
    wa: WhatsAppClient = state.wa
    router: EventRouter = state.router

    try:
        session = router.open_call(body.to_number, "outbound")
    except SessionConflict:
        raise HTTPException(409, f"{body.to_number} already on a call") from None

    session_id = uuid.uuid4().hex
    room = f"wa-{session_id[:8]}"
    bridge = RoomCallBridge()
    try:
        if body.request_permission:
            await wa.send_permission_request(body.to_number, "May we call you?")
            if not await session.wait_permission():
                raise HTTPException(403, "call permission denied")

        await _dispatch_and_join(settings, bridge, room, body.agent_id,
                                 session_id, body.to_number, body.prompt_vars)
        offer = munge_sdp_for_meta(await bridge.create_offer())
        session.call_id = _extract_call_id(await wa.initiate_call(body.to_number, offer))
        answer = await session.wait_call_answer(body.answer_timeout_s)
        await bridge.set_answer(answer.sdp)
    except Exception:
        await bridge.close()
        router.close_call(session)
        raise

    state.bridges[session_id] = bridge
    _spawn_watch(state, session_id, session)
    return {"session_id": session_id, "room": room, "call_id": session.call_id}


async def _drain_inbound(app: FastAPI) -> None:
    """Answer inbound calls: each SDP offer -> dispatch agent + bridge + accept."""
    state = app.state
    router: EventRouter = state.router
    while True:
        event: CallEvent = await router.incoming_calls.get()
        try:
            await _answer_inbound(state, event)
        except Exception:
            logger.exception("failed to answer inbound call %s", event.call_id)


async def _answer_inbound(state: Any, event: CallEvent) -> None:
    settings: Settings = state.settings
    wa: WhatsAppClient = state.wa
    router: EventRouter = state.router
    agent_id = settings.whatsapp.default_agent
    if not agent_id or agent_id not in state.agents:
        logger.error("inbound call but WHATSAPP_DEFAULT_AGENT is unset/unknown; rejecting")
        if event.call_id:
            await wa.terminate_call(event.call_id)
        return
    try:
        session = router.open_call(event.from_number, "inbound")
    except SessionConflict:
        logger.warning("inbound call from %s but a session is already open", event.from_number)
        return
    session.call_id = event.call_id

    session_id = uuid.uuid4().hex
    room = f"wa-{session_id[:8]}"
    bridge = RoomCallBridge()
    try:
        await _dispatch_and_join(settings, bridge, room, agent_id,
                                 session_id, event.from_number, {})
        answer = munge_sdp_for_meta(await bridge.create_answer(event.sdp))
        await wa.pre_accept_call(event.call_id, answer)
        await wa.accept_call(event.call_id, answer)
    except Exception:
        await bridge.close()
        router.close_call(session)
        raise
    state.bridges[session_id] = bridge
    _spawn_watch(state, session_id, session)
    logger.info("inbound call answered (call_id=%s, room=%s)", event.call_id, room)


async def _dispatch_and_join(
    settings: Settings,
    bridge: RoomCallBridge,
    room: str,
    agent_id: str,
    session_id: str,
    peer: str,
    prompt_vars: dict[str, str],
) -> None:
    meta = SessionMetadata(
        agent_id=agent_id,
        session_id=session_id,
        channel="whatsapp",
        user_id=peer,
        call_id=session_id,
        prompt_vars=prompt_vars,
    )
    await dispatch_agent(
        settings.livekit,
        room=room,
        worker_name=settings.agent_source.worker_name,
        metadata=meta,
    )
    token = mint_join_token(
        settings.livekit,
        room=room,
        identity=f"wa-bridge-{session_id[:8]}",
        can_publish=True,
        can_subscribe=True,
    )
    await bridge.connect_room(settings.livekit.url, token)


def _spawn_watch(state: Any, session_id: str, session: CallSession) -> None:
    """Fire-and-forget end-of-call cleanup, keeping a reference so the task
    isn't garbage-collected mid-flight."""
    task = asyncio.create_task(_watch_end(state, session_id, session))
    state.watchers.add(task)
    task.add_done_callback(state.watchers.discard)


async def _watch_end(state: Any, session_id: str, session: CallSession) -> None:
    await session.ended.wait()
    logger.info("call ended (session=%s)", session_id)
    bridge = state.bridges.pop(session_id, None)
    if bridge is not None:
        await bridge.close()
    state.router.close_call(session)
    room = f"wa-{session_id[:8]}"
    await delete_room(state.settings.livekit, room)


def main() -> None:
    """Console script `voiceagent-whatsapp`."""
    import uvicorn

    logging.basicConfig(level=logging.INFO)
    settings = load_settings()
    missing = settings.whatsapp.require_credentials()
    if missing:
        raise SystemExit(f"cannot start whatsapp service; missing: {', '.join(missing)}")
    uvicorn.run(
        create_app(settings),
        host="0.0.0.0",
        port=settings.whatsapp.port,
        log_level="info",
    )
