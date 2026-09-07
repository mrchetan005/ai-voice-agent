"""Outbound call API: start a call from any client (the consumer agent,
a CRM hook, curl) without touching the CLI.

    POST /calls {to?, provider?, brain?, voice?, skip_permission?, brief?}
        202 {"call_ref": ...}   call started in the background
        200 {"call_ref": ...}   Idempotency-Key replay (same ref, no new call)
        409                     peer already on a call / at capacity
    GET /calls/{call_ref}
        200 {call_ref, status, peer, updated_at, reason?} | 404 unknown

brief = {topic, time_range?: {from, to}, attendee_name?, attendee_email?,
timezone?} — carried into the agent so it opens with the topic, offers only
slots inside the window and pre-fills the attendee (details still confirmed
aloud). Omitted engine fields resolve through runtime config, then Settings.
"""

from __future__ import annotations

import datetime as dt
import logging
import re
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, ValidationError

from whatsapp_agent.api.deps import require_admin

logger = logging.getLogger("whatsapp_agent")

router = APIRouter(dependencies=[Depends(require_admin)])

_IDEM_TTL_S = 86_400
_PHONE_RE = re.compile(r"\+?[1-9][0-9]{6,14}")


class TimeRangeIn(BaseModel):
    model_config = {"populate_by_name": True}

    from_: dt.datetime = Field(alias="from")
    to: dt.datetime


class BriefIn(BaseModel):
    topic: str = Field(min_length=1, max_length=200)
    time_range: TimeRangeIn | None = None
    attendee_name: str = ""
    attendee_email: str = ""
    timezone: str = ""


class StartCallIn(BaseModel):
    to: str | None = None
    provider: str | None = None
    brain: str | None = None
    voice: str | None = None
    skip_permission: bool = False
    brief: BriefIn | None = None


def _to_call_brief(brief: BriefIn) -> tuple:
    """(CallBrief, None) or (None, "error message" for the 422 body)."""
    from whatsapp_agent.capabilities.booking.service import EMAIL_RE
    from whatsapp_agent.channels.call_manager import CallBrief
    from whatsapp_agent.config import get_settings

    email = brief.attendee_email.strip()
    if email and not EMAIL_RE.fullmatch(email):
        return None, "attendee_email is not a valid email address"
    tz_name = brief.timezone.strip() or get_settings().cal_timezone
    try:
        tz = ZoneInfo(tz_name)
    except Exception:
        return None, f"unknown timezone {brief.timezone!r}"
    start = end = None
    if brief.time_range is not None:
        start, end = brief.time_range.from_, brief.time_range.to
        if start.tzinfo is None:  # naive datetimes are in the brief's timezone
            start = start.replace(tzinfo=tz)
        if end.tzinfo is None:
            end = end.replace(tzinfo=tz)
        if start >= end:
            return None, "time_range.from must be before time_range.to"
        if end <= dt.datetime.now(dt.UTC):
            return None, "time_range.to is in the past"
    return CallBrief(
        topic=brief.topic.strip(),
        window_start=start,
        window_end=end,
        attendee_name=brief.attendee_name.strip(),
        attendee_email=email,
        timezone=tz_name,
    ), None


@router.post("/calls")
async def start_call(request: Request) -> JSONResponse:
    from whatsapp_agent.channels.call_manager import PROVIDERS, CallBusy

    calls = getattr(request.app.state, "calls", None)
    if calls is None:
        return JSONResponse({"error": "call manager not running"}, status_code=503)
    try:
        body = await request.json()
    except Exception:
        body = {}
    if not isinstance(body, dict):
        body = {}
    try:
        req = StartCallIn.model_validate(body)
    except ValidationError as exc:
        first = exc.errors()[0]
        loc = ".".join(str(part) for part in first.get("loc", ()))
        message = f"{loc}: {first.get('msg', 'invalid')}" if loc else str(first.get("msg", "invalid body"))
        return JSONResponse({"error": message}, status_code=422)

    # Enum fields fail HERE with a 422, not later inside the background task.
    if req.provider is not None and req.provider not in PROVIDERS:
        return JSONResponse(
            {"error": f"provider must be one of {PROVIDERS}"}, status_code=422
        )
    if req.brain is not None and req.brain not in ("single", "dual"):
        return JSONResponse({"error": "brain must be single or dual"}, status_code=422)
    if req.to is not None and not _PHONE_RE.fullmatch(req.to.strip()):
        return JSONResponse({"error": "to must be an E.164 phone number"}, status_code=422)
    brief = None
    if req.brief is not None:
        brief, err = _to_call_brief(req.brief)
        if err:
            return JSONResponse({"error": err}, status_code=422)

    redis = getattr(request.app.state, "redis", None)
    idem = request.headers.get("Idempotency-Key", "")
    if idem and redis is not None and (
        previous := await redis.get_json(f"idem:calls:{idem}")
    ):
        return JSONResponse(previous, status_code=200)

    try:
        call_ref = await calls.start_outbound(
            peer=req.to.strip() if req.to else None,
            provider=req.provider,
            brain=req.brain,
            voice=req.voice,
            skip_permission=req.skip_permission,
            brief=brief,
        )
    except CallBusy as exc:
        message = "at capacity" if str(exc) == "at capacity" else "another call is active"
        return JSONResponse({"error": message}, status_code=409)

    if idem and redis is not None:
        await redis.set_json(f"idem:calls:{idem}", {"call_ref": call_ref}, ttl_s=_IDEM_TTL_S)
    return JSONResponse({"call_ref": call_ref}, status_code=202)


@router.get("/calls/{call_ref}")
async def call_status(call_ref: str, request: Request) -> JSONResponse:
    """Status of an API-started call: accepted -> permission_pending? ->
    dialing -> ringing -> in_progress -> completed | failed | no_answer |
    permission_denied. Redis-backed (fail-open), keys expire after 24 h."""
    redis = getattr(request.app.state, "redis", None)
    data = await redis.get_json(f"call:status:{call_ref}") if redis is not None else None
    if not data:
        return JSONResponse({"error": "unknown call_ref"}, status_code=404)
    return JSONResponse(data, status_code=200)
