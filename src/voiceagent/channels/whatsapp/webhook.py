"""Meta webhook plumbing: verify handshake, signature check, call routing.

Trimmed from the production receiver on `main` to just the call surface this
framework needs (no chat/booking lanes). Parsing is defensive — Meta's
payload shapes vary across rollout phases — and the HTTP handler must answer
200 fast, so routing is pure sync enqueue and never awaits.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
from dataclasses import dataclass, field
from typing import Any, Literal

from pydantic import BaseModel

logger = logging.getLogger("voiceagent")


def verify_subscription(mode: str, token: str, expected_token: str) -> bool:
    """GET handshake: Meta calls with hub.mode=subscribe + hub.verify_token."""
    return mode == "subscribe" and bool(expected_token) and hmac.compare_digest(
        token, expected_token
    )


def verify_signature(body: bytes, header: str | None, app_secret: str) -> bool:
    """Validate X-Hub-Signature-256. If no app_secret is configured, skip
    (returns True) — a dev convenience, not a production posture."""
    if not app_secret:
        return True
    if not header or not header.startswith("sha256="):
        return False
    digest = hmac.new(app_secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(header[len("sha256="):], digest)


class CallEvent(BaseModel):
    """SDP-bearing `calls` webhook: an offer (user calls us) or an answer
    (reply to our outbound offer)."""

    call_id: str = ""
    sdp: str
    sdp_type: str = "answer"
    from_number: str = ""
    direction: str = ""


@dataclass
class CallSession:
    """Everything one live call waits on. Keyed by peer; matched to lifecycle
    events strictly by call_id."""

    peer: str
    direction: Literal["inbound", "outbound"]
    call_id: str = ""
    answers: asyncio.Queue[CallEvent] = field(default_factory=lambda: asyncio.Queue(maxsize=8))
    permission: asyncio.Queue[bool] = field(default_factory=lambda: asyncio.Queue(maxsize=8))
    accepted: asyncio.Event = field(default_factory=asyncio.Event)
    ended: asyncio.Event = field(default_factory=asyncio.Event)

    async def wait_call_answer(self, timeout_s: float = 30.0) -> CallEvent:
        return await asyncio.wait_for(self.answers.get(), timeout_s)

    async def wait_permission(self, timeout_s: float = 300.0) -> bool:
        return await asyncio.wait_for(self.permission.get(), timeout_s)


class SessionConflict(RuntimeError):
    """Peer already has an open call session in this process."""


class EventRouter:
    """Meta payloads in, routed to per-call sessions out. Inbound offers land
    on `incoming_calls` for the service to answer."""

    def __init__(self) -> None:
        self.incoming_calls: asyncio.Queue[CallEvent] = asyncio.Queue(maxsize=8)
        self._sessions: dict[str, CallSession] = {}

    def open_call(self, peer: str, direction: Literal["inbound", "outbound"]) -> CallSession:
        peer = peer.lstrip("+")
        if peer in self._sessions:
            raise SessionConflict(peer)
        session = CallSession(peer=peer, direction=direction)
        self._sessions[peer] = session
        return session

    def close_call(self, session: CallSession) -> None:
        self._sessions.pop(session.peer, None)

    def _session_for(self, call_id: str = "", peer: str = "") -> CallSession | None:
        """Match a CALL event to a session. Sole-active fallback exists because
        Meta omits ids on some status shapes — safe for accepted, never for END
        (see _session_by_call_id)."""
        if peer and (session := self._sessions.get(peer.lstrip("+"))):
            return session
        if call_id:
            for session in self._sessions.values():
                if session.call_id and session.call_id == call_id:
                    return session
        if len(self._sessions) == 1:
            return next(iter(self._sessions.values()))
        return None

    def _session_by_call_id(self, call_id: str) -> CallSession | None:
        """Strict call-id match for lifecycle-END events. Meta redelivers a
        previous call's terminate late (a different call_id), so this must
        NEVER fall back to the sole active session — that would kill the live
        call. Returns None until the session's call_id is known."""
        if not call_id:
            return None
        for session in self._sessions.values():
            if session.call_id and session.call_id == call_id:
                return session
        return None

    def dispatch(self, payload: dict[str, Any]) -> None:
        logger.info("webhook: %s", json.dumps(payload)[:800])
        for entry in payload.get("entry", []):
            for change in entry.get("changes", []):
                self._route(change.get("value", {}) or {})

    def _route(self, value: dict[str, Any]) -> None:
        for call in value.get("calls", []) or []:
            self._route_call(call)
        for message in value.get("messages", []) or []:
            self._route_message(message)
        for status in value.get("statuses", []) or []:
            self._route_status(status)

    def _route_status(self, status: dict[str, Any]) -> None:
        if str(status.get("type", "")).lower() != "call" and "call" not in str(status)[:200].lower():
            return
        state = str(status.get("status", "")).lower()
        status_id = str(status.get("id", ""))
        if state == "accepted" and (session := self._session_for(call_id=status_id)):
            session.accepted.set()
        elif state in ("terminated", "failed", "rejected", "ended", "completed") and (
            session := self._session_by_call_id(status_id)
        ):
            session.ended.set()

    def _route_call(self, call: dict[str, Any]) -> None:
        event = str(call.get("event") or call.get("status") or "").lower()
        call_id = str(call.get("id") or call.get("call_id") or "")
        session_sdp = call.get("session") or {}
        sdp = session_sdp.get("sdp")
        if sdp:
            payload = CallEvent(
                call_id=call_id,
                sdp=sdp,
                sdp_type=session_sdp.get("sdp_type", "answer"),
                from_number=call.get("from", ""),
                direction=call.get("direction", ""),
            )
            # OFFER = a user is calling US (inbound); ANSWER = reply to our offer.
            if payload.sdp_type == "offer" or payload.direction == "USER_INITIATED":
                _put(self.incoming_calls, payload)
            elif session := self._session_for(call_id, payload.from_number):
                session.call_id = session.call_id or call_id
                _put(session.answers, payload)
            else:
                logger.warning("SDP answer with no open call session; dropped")
        if event in ("terminate", "terminated", "failed", "rejected", "ended") and (
            session := self._session_by_call_id(call_id)
        ):
            session.ended.set()

    def _route_message(self, message: dict[str, Any]) -> None:
        if message.get("type") != "interactive":
            return
        peer = message.get("from", "")
        session = self._sessions.get(peer.lstrip("+")) if peer else None
        interactive = message.get("interactive") or {}
        reply = interactive.get("call_permission_reply") or {}
        if reply or interactive.get("type") == "call_permission_reply":
            verdict = str(reply.get("response", "")).lower() in ("accept", "accepted")
            if session is not None:
                _put(session.permission, verdict)
            return
        # Unknown shapes: look for the verdict anywhere (button payloads use
        # ACCEPTED/REJECTED).
        blob = json.dumps(message).lower()
        if ("call_permission" in blob or "voice_call_request" in blob) and session is not None:
            _put(session.permission, "accept" in blob)


def _put(queue: asyncio.Queue[Any], item: Any) -> None:
    try:
        queue.put_nowait(item)
    except asyncio.QueueFull:
        logger.warning("event queue full; dropping %r", item)
