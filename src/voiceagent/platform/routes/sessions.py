"""Session lifecycle: create (token + agent dispatch), status, end."""

from __future__ import annotations

import uuid

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from voiceagent.livekit.api import delete_room, mint_join_token
from voiceagent.metadata import SessionMetadata
from voiceagent.observability import metrics

router = APIRouter()


class CreateSessionIn(BaseModel):
    agent_id: str
    channel: str = "browser"
    user_id: str | None = None
    prompt_vars: dict[str, str] = Field(default_factory=dict)
    memory_key: str | None = None
    record: bool = False
    language: str | None = None


class CreateSessionOut(BaseModel):
    session_id: str
    room: str
    livekit_url: str
    token: str


@router.post("/v1/sessions", status_code=201, response_model=CreateSessionOut)
async def create_session(body: CreateSessionIn, request: Request) -> CreateSessionOut:
    state = request.app.state
    if body.agent_id not in state.agents:
        raise HTTPException(404, f"unknown agent {body.agent_id!r}; see GET /v1/agents")

    session_id = uuid.uuid4().hex
    room = f"va-{body.agent_id}-{session_id[:8]}"
    meta = SessionMetadata(
        agent_id=body.agent_id,
        session_id=session_id,
        channel=body.channel,
        user_id=body.user_id,
        prompt_vars=body.prompt_vars,
        memory_key=body.memory_key,
        record=body.record,
        language=body.language,
    )
    token = mint_join_token(
        state.settings.livekit,
        room=room,
        identity=body.user_id or f"user-{session_id[:8]}",
        worker_name=state.settings.agent_source.worker_name,
        metadata=meta,
    )
    await state.status.update(
        session_id, status="pending", agent_id=body.agent_id, room=room, channel=body.channel
    )
    await state.store.insert(
        session_id=session_id,
        tenant_id=meta.tenant_id,
        agent_id=body.agent_id,
        channel=body.channel,
        room=room,
        user_id=body.user_id,
    )
    metrics.session_created(body.agent_id, body.channel)
    return CreateSessionOut(
        session_id=session_id,
        room=room,
        livekit_url=state.settings.livekit.effective_public_url,
        token=token,
    )


@router.get("/v1/sessions/{session_id}")
async def get_session(session_id: str, request: Request) -> dict:
    state = request.app.state
    status = await state.status.get(session_id)
    record = await state.store.get(session_id)
    if status is None and record is None:
        raise HTTPException(404, "unknown session")
    out: dict = {"session_id": session_id}
    if record:
        out.update({k: v for k, v in record.items() if k != "id"})
    if status:
        out.update(status)
    return out


@router.delete("/v1/sessions/{session_id}", status_code=204)
async def end_session(session_id: str, request: Request) -> None:
    state = request.app.state
    status = await state.status.get(session_id)
    if not status or "room" not in status:
        raise HTTPException(404, "unknown or already-ended session")
    await delete_room(state.settings.livekit, status["room"])
    await state.status.update(session_id, status="ending")
