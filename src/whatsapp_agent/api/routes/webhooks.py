"""Meta webhook ingress: verify handshake + signed event receiver.

The POST handler must stay O(1) and always answer 200 fast — Meta retries
aggressively on anything else. Order: raw bytes -> HMAC signature (when
WHATSAPP_APP_SECRET is set) -> per-item dedup + rate limit (Redis,
fail-open) -> EventRouter.dispatch (pure sync enqueue) -> 200.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging

from fastapi import APIRouter, Request, Response
from fastapi.responses import JSONResponse, PlainTextResponse

from whatsapp_agent.config import get_settings

logger = logging.getLogger("whatsapp_agent")

router = APIRouter()


@router.get("/webhook")
async def verify(request: Request) -> PlainTextResponse:
    params = request.query_params
    if (
        params.get("hub.mode") == "subscribe"
        and params.get("hub.verify_token") == get_settings().whatsapp_verify_token
    ):
        return PlainTextResponse(params.get("hub.challenge", ""))
    return PlainTextResponse("verify token mismatch", status_code=403)


def _signature_ok(secret: str, raw: bytes, header: str) -> bool:
    expected = "sha256=" + hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, header)


@router.post("/webhook")
async def receive(request: Request) -> Response:
    raw = await request.body()
    secret = get_settings().whatsapp_app_secret
    if secret and not _signature_ok(secret, raw, request.headers.get("X-Hub-Signature-256", "")):
        logger.warning("webhook signature mismatch; rejected")
        return JSONResponse({"error": "bad signature"}, status_code=403)
    try:
        payload = json.loads(raw or b"{}")
    except json.JSONDecodeError:
        logger.warning("webhook body was not JSON; ignored")
        return PlainTextResponse("ok")
    gate = getattr(request.app.state, "webhook_gate", None)
    if gate is not None:
        payload = await gate(payload)  # dedup + rate limit (Redis, fail-open)
        if payload is None:
            return PlainTextResponse("ok")
    request.app.state.router.dispatch(payload)
    return PlainTextResponse("ok")
