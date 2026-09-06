"""Runtime config API: read and change engine settings without env edits.

    GET  /config           -> {key: {value, source}}  source: runtime|default
    POST /config {k: v}    -> updated snapshot; v=null clears the override

Example: POST {"provider": "split", "voice": "kavita"} makes the NEXT call
use the split stack — no restart, no .env change.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse

from whatsapp_agent.api.deps import require_admin

router = APIRouter(dependencies=[Depends(require_admin)])


@router.get("/config")
async def get_config(request: Request) -> JSONResponse:
    config = getattr(request.app.state, "config", None)
    if config is None:
        return JSONResponse({"error": "config store not running"}, status_code=503)
    return JSONResponse(await config.snapshot())


@router.post("/config")
async def set_config(request: Request) -> JSONResponse:
    config = getattr(request.app.state, "config", None)
    if config is None:
        return JSONResponse({"error": "config store not running"}, status_code=503)
    if config.degraded:
        # Without the DB an override would silently vanish — refuse honestly.
        return JSONResponse(
            {"error": "database unavailable; config changes cannot persist"},
            status_code=503,
        )
    try:
        body = await request.json()
    except Exception:
        body = None
    if not isinstance(body, dict) or not body:
        return JSONResponse(
            {"error": "body must be a JSON object of {key: value}"}, status_code=422
        )
    try:
        for key, value in body.items():
            await config.set(key, value)
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=422)
    return JSONResponse(await config.snapshot())
