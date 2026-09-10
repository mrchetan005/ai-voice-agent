"""Outbound SIP calls: dispatch an agent into a room, then dial the callee."""

from __future__ import annotations

import uuid

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from voiceagent.livekit.api import create_sip_outbound, dispatch_agent
from voiceagent.metadata import SessionMetadata

router = APIRouter()


class SipCallIn(BaseModel):
    agent_id: str
    to_number: str
    trunk_id: str | None = None  # falls back to PLATFORM_SIP_TRUNK_ID
    user_id: str | None = None
    prompt_vars: dict[str, str] = Field(default_factory=dict)
    record: bool = False


@router.post("/v1/calls/sip", status_code=201)
async def create_sip_call(body: SipCallIn, request: Request) -> dict:
    state = request.app.state
    if body.agent_id not in state.agents:
        raise HTTPException(404, f"unknown agent {body.agent_id!r}")
    trunk_id = body.trunk_id or state.settings.platform.sip_trunk_id
    if not trunk_id:
        raise HTTPException(400, "no SIP trunk configured (PLATFORM_SIP_TRUNK_ID or trunk_id)")

    session_id = uuid.uuid4().hex
    room = f"sip-out-{session_id[:8]}"
    meta = SessionMetadata(
        agent_id=body.agent_id,
        session_id=session_id,
        channel="sip",
        user_id=body.user_id or body.to_number,
        call_id=session_id,
        prompt_vars=body.prompt_vars,
        record=body.record,
    )
    await dispatch_agent(
        state.settings.livekit,
        room=room,
        worker_name=state.settings.agent_source.worker_name,
        metadata=meta,
    )
    participant_id = await create_sip_outbound(
        state.settings.livekit,
        trunk_id=trunk_id,
        to_number=body.to_number,
        room=room,
        identity=f"phone-{body.to_number}",
    )
    await state.status.update(
        session_id, status="dialing", agent_id=body.agent_id, room=room, channel="sip"
    )
    await state.store.insert(
        session_id=session_id,
        tenant_id=meta.tenant_id,
        agent_id=body.agent_id,
        channel="sip",
        room=room,
        user_id=meta.user_id,
    )
    return {"session_id": session_id, "room": room, "sip_participant": participant_id}
