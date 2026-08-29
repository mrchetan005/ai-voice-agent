"""Meta webhook receiver (aiohttp) for the `calls` and `messages` fields.

Expose publicly (ngrok http 8080), configure the URL + WHATSAPP_VERIFY_TOKEN
in the Meta App dashboard, subscribe to BOTH `calls` and `messages`.

Parsing is deliberately defensive: Meta's calling webhook shapes vary across
rollout phases, so every payload is logged (truncated) and the extractors
look for the semantic bits (sdp, permission response, text body) rather than
assuming one exact envelope. Stage-3 verification pins the real shapes.

Serve-only mode for webhook setup:
    uv run --env-file .env appointment-booker --serve-only
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from typing import Any

from aiohttp import web
from pydantic import BaseModel

logger = logging.getLogger("appointment_booker")


class CallEvent(BaseModel):
    """SDP-bearing `calls` webhook: an offer (user calls us) or an answer
    (reply to our outbound offer)."""

    call_id: str = ""
    sdp: str
    sdp_type: str = "answer"
    from_number: str = ""
    direction: str = ""


class TextMessage(BaseModel):
    """Inbound WhatsApp text message."""

    from_number: str = ""
    text: str = ""


class WebhookHub:
    def __init__(self, verify_token: str | None = None, port: int = 8080) -> None:
        self._verify_token = verify_token or os.environ.get("WHATSAPP_VERIFY_TOKEN", "voiceagent")
        self._port = port
        self.call_answers: asyncio.Queue[CallEvent] = asyncio.Queue(maxsize=8)
        # Inbound (user-initiated) calls: connect events carrying an SDP OFFER.
        self.incoming_calls: asyncio.Queue[CallEvent] = asyncio.Queue(maxsize=8)
        self.permission_results: asyncio.Queue[bool] = asyncio.Queue(maxsize=8)
        self.text_messages: asyncio.Queue[TextMessage] = asyncio.Queue(maxsize=32)
        self.call_ended = asyncio.Event()
        # Set when the callee actually picks up (status ACCEPTED) — greeting
        # must wait for this, or the first words play into the ringtone.
        self.call_accepted = asyncio.Event()
        self._runner: web.AppRunner | None = None

    # -- http ----------------------------------------------------------------

    async def _verify(self, request: web.Request) -> web.Response:
        params = request.query
        if (
            params.get("hub.mode") == "subscribe"
            and params.get("hub.verify_token") == self._verify_token
        ):
            return web.Response(text=params.get("hub.challenge", ""))
        return web.Response(status=403, text="verify token mismatch")

    async def _receive(self, request: web.Request) -> web.Response:
        payload = await request.json()
        logger.info("webhook: %s", json.dumps(payload)[:800])
        for entry in payload.get("entry", []):
            for change in entry.get("changes", []):
                self._route(change.get("field", ""), change.get("value", {}) or {})
        # Always 200 fast — Meta retries aggressively on anything else.
        return web.Response(text="ok")

    # -- routing ---------------------------------------------------------------

    def _route(self, field: str, value: dict[str, Any]) -> None:
        for call in value.get("calls", []) or []:
            self._route_call(call)
        for message in value.get("messages", []) or []:
            self._route_message(message)
        # Some payloads deliver call status under `statuses` instead.
        for status in value.get("statuses", []) or []:
            if str(status.get("type", "")).lower() == "call" or "call" in str(status)[:200].lower():
                state = str(status.get("status", "")).lower()
                if state == "accepted":
                    self.call_accepted.set()
                elif state in ("terminated", "failed", "rejected", "ended", "completed"):
                    self.call_ended.set()

    def _route_call(self, call: dict[str, Any]) -> None:
        event = str(call.get("event") or call.get("status") or "").lower()
        session = call.get("session") or {}
        sdp = session.get("sdp")
        if sdp:
            payload = CallEvent(
                call_id=str(call.get("id") or call.get("call_id") or ""),
                sdp=sdp,
                sdp_type=session.get("sdp_type", "answer"),
                from_number=call.get("from", ""),
                direction=call.get("direction", ""),
            )
            # OFFER = a user is calling US (inbound); ANSWER = reply to our
            # outbound offer.
            if payload.sdp_type == "offer" or payload.direction == "USER_INITIATED":
                self._put(self.incoming_calls, payload)
            else:
                self._put(self.call_answers, payload)
        if event in ("terminate", "terminated", "failed", "rejected", "ended"):
            self.call_ended.set()

    def _route_message(self, message: dict[str, Any]) -> None:
        mtype = message.get("type")
        if mtype == "text":
            self._put(self.text_messages, TextMessage(
                from_number=message.get("from", ""),
                text=(message.get("text") or {}).get("body", ""),
            ))
            return
        if mtype == "interactive":
            interactive = message.get("interactive") or {}
            reply = interactive.get("call_permission_reply") or {}
            if reply or interactive.get("type") == "call_permission_reply":
                response = str(reply.get("response", "")).lower()
                self._put(self.permission_results, response in ("accept", "accepted"))
                return
        # Unknown interactive/button shapes: look for the permission verdict
        # anywhere in the message (button payloads use ACCEPTED/REJECTED).
        blob = json.dumps(message).lower()
        if "call_permission" in blob or "voice_call_request" in blob:
            self._put(self.permission_results, "accept" in blob)

    @staticmethod
    def _put(queue: asyncio.Queue[Any], item: Any) -> None:
        try:
            queue.put_nowait(item)
        except asyncio.QueueFull:
            logger.warning("webhook queue full; dropping %r", item)

    # -- lifecycle + waiters --------------------------------------------------

    async def start(self) -> None:
        app = web.Application()
        app.router.add_get("/webhook", self._verify)
        app.router.add_post("/webhook", self._receive)
        self._runner = web.AppRunner(app)
        await self._runner.setup()
        site = web.TCPSite(self._runner, "0.0.0.0", self._port)
        await site.start()
        logger.info("webhook server on :%d/webhook", self._port)

    async def stop(self) -> None:
        if self._runner is not None:
            await self._runner.cleanup()
            self._runner = None

    async def wait_call_answer(self, timeout_s: float = 30.0) -> CallEvent:
        return await asyncio.wait_for(self.call_answers.get(), timeout_s)

    async def wait_permission(self, timeout_s: float = 300.0) -> bool:
        return await asyncio.wait_for(self.permission_results.get(), timeout_s)

    async def wait_text(self, timeout_s: float = 60.0) -> TextMessage:
        return await asyncio.wait_for(self.text_messages.get(), timeout_s)


