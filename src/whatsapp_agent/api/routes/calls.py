"""Outbound call API: start a call from any client (the consumer agent,
a CRM hook, curl) without touching the CLI.

    POST /calls {to?, provider?, brain?, voice?, skip_permission?}
        202 {"call_ref": ...}   call started in the background
        200 {"call_ref": ...}   Idempotency-Key replay (same ref, no new call)
        409                     another call is active

Omitted fields resolve through runtime config, then Settings.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse

from whatsapp_agent.api.deps import require_admin

logger = logging.getLogger("whatsapp_agent")

router = APIRouter(dependencies=[Depends(require_admin)])

_IDEM_TTL_S = 86_400


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

    # Enum fields fail HERE with a 422, not later inside the background task.
    provider, brain = body.get("provider"), body.get("brain")
    if provider is not None and provider not in PROVIDERS:
        return JSONResponse(
            {"error": f"provider must be one of {PROVIDERS}"}, status_code=422
        )
    if brain is not None and brain not in ("single", "dual"):
        return JSONResponse({"error": "brain must be single or dual"}, status_code=422)

    redis = getattr(request.app.state, "redis", None)
    idem = request.headers.get("Idempotency-Key", "")
    if idem and redis is not None and (
        previous := await redis.get_json(f"idem:calls:{idem}")
    ):
        return JSONResponse(previous, status_code=200)

    try:
        call_ref = calls.start_outbound(
            peer=body.get("to"),
            provider=provider,
            brain=brain,
            voice=body.get("voice"),
            skip_permission=bool(body.get("skip_permission", False)),
        )
    except CallBusy:
        return JSONResponse({"error": "another call is active"}, status_code=409)

    if idem and redis is not None:
        await redis.set_json(f"idem:calls:{idem}", {"call_ref": call_ref}, ttl_s=_IDEM_TTL_S)
    return JSONResponse({"call_ref": call_ref}, status_code=202)
