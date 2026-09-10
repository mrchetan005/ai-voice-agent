"""Liveness and readiness."""

from __future__ import annotations

from fastapi import APIRouter, Request, Response

router = APIRouter()


@router.get("/healthz")
async def healthz() -> dict:
    return {"status": "ok"}


@router.get("/readyz")
async def readyz(request: Request, response: Response) -> dict:
    """Ready = LiveKit reachable. Redis/PG are fail-open, reported not gating."""
    import httpx

    state = request.app.state
    lk = state.settings.livekit
    url = lk.url.replace("ws://", "http://").replace("wss://", "https://")
    checks: dict[str, str] = {}
    try:
        async with httpx.AsyncClient(timeout=2.0) as client:
            resp = await client.get(url)
        checks["livekit"] = "ok" if resp.status_code < 500 else f"http {resp.status_code}"
    except Exception as exc:
        checks["livekit"] = f"unreachable: {exc.__class__.__name__}"
        response.status_code = 503
    checks["redis"] = await state.status.ping()
    return {"status": "ready" if response.status_code != 503 else "not-ready", "checks": checks}
