"""Shared FastAPI dependencies."""

from __future__ import annotations

from fastapi import HTTPException, Request

from whatsapp_agent.config import get_settings


def require_admin(request: Request) -> None:
    """Bearer auth for the mutating admin surface (/config, /calls).
    Cloaked like /report and /costs: with no token configured the
    endpoints answer 404, indistinguishable from not existing — they
    share the public HTTPS ingress with the Meta webhook."""
    settings = get_settings()
    token = settings.admin_token or settings.metrics_token
    if not token:
        raise HTTPException(status_code=404, detail="not found")
    if request.headers.get("Authorization", "") != f"Bearer {token}":
        raise HTTPException(status_code=401, detail="unauthorized")
