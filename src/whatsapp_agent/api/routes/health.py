"""Liveness/readiness endpoint (no auth): used by Docker healthchecks."""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

router = APIRouter()


@router.get("/health")
async def health(request: Request) -> JSONResponse:
    state = request.app.state
    redis_gateway = getattr(state, "redis", None)
    if redis_gateway is None or not redis_gateway.enabled:
        redis_status = "disabled"
    else:
        redis_status = "ok" if await redis_gateway.ping() else "down"
    session_store = getattr(state, "session_store", None)
    calls = getattr(state, "calls", None)
    return JSONResponse({
        "status": "ok",
        "redis": redis_status,
        "db": "degraded" if session_store is None or session_store.degraded else "ok",
        "active_call": bool(calls is not None and calls.call_active),
        "version": "0.1.0",
    })
