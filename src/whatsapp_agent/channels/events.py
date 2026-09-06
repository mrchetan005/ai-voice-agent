"""WhatsApp event plumbing: webhook payloads -> per-call sessions or chat.

Three layers, one file, because they share the queue vocabulary:

- ``EventRouter`` — parses Meta webhook payloads (defensively: shapes vary
  across rollout phases) and routes each event either to the ``CallSession``
  of the peer currently on a call, or to the chat lanes.
- ``CallSession`` — everything one live call waits on (SDP answers,
  permission taps, texts, buttons, accepted/ended events). Satisfies
  ``InboundWaiter``, the ONLY protocol the booking domain sees.

One call at a time is a CallManager policy, not a structural limit here:
sessions are keyed by peer and matched by call_id, so concurrent calls are
a lock removal away.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol

from pydantic import BaseModel

logger = logging.getLogger("whatsapp_agent")

_EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+\.[\w.]+")


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


class ButtonReply(BaseModel):
    """User tapped a reply button on an interactive message."""

    from_number: str = ""
    button_id: str = ""
    title: str = ""


class InboundWaiter(Protocol):
    """The only inbound-message surface domain code may depend on."""

    async def wait_text(self, timeout_s: float = 60.0) -> TextMessage: ...
    async def wait_button(self, timeout_s: float = 60.0) -> ButtonReply: ...


class NullWaiter:
    """Chat sessions: the chat loop consumes taps itself, so domain waits
    simply time out instead of contending for the queue."""

    async def wait_text(self, timeout_s: float = 60.0) -> TextMessage:
        await asyncio.sleep(timeout_s)
        raise TimeoutError

    async def wait_button(self, timeout_s: float = 60.0) -> ButtonReply:
        await asyncio.sleep(timeout_s)
        raise TimeoutError


@dataclass
class CallSession:
    """Everything one live call waits on. While a session is open, the
    router redirects the peer's texts/buttons here — the single-consumer
    invariant that used to be implicit in TextBridge."""

    peer: str
    direction: Literal["inbound", "outbound"]
    call_id: str = ""
    call_ref: str = ""
    answers: asyncio.Queue[CallEvent] = field(default_factory=lambda: asyncio.Queue(maxsize=8))
    permission: asyncio.Queue[bool] = field(default_factory=lambda: asyncio.Queue(maxsize=8))
    texts: asyncio.Queue[TextMessage] = field(default_factory=lambda: asyncio.Queue(maxsize=32))
    buttons: asyncio.Queue[ButtonReply] = field(default_factory=lambda: asyncio.Queue(maxsize=16))
    accepted: asyncio.Event = field(default_factory=asyncio.Event)
    ended: asyncio.Event = field(default_factory=asyncio.Event)

    async def wait_call_answer(self, timeout_s: float = 30.0) -> CallEvent:
        return await asyncio.wait_for(self.answers.get(), timeout_s)

    async def wait_permission(self, timeout_s: float = 300.0) -> bool:
        return await asyncio.wait_for(self.permission.get(), timeout_s)

    async def wait_text(self, timeout_s: float = 60.0) -> TextMessage:
        return await asyncio.wait_for(self.texts.get(), timeout_s)

    async def wait_button(self, timeout_s: float = 60.0) -> ButtonReply:
        return await asyncio.wait_for(self.buttons.get(), timeout_s)


class EventRouter:
    """Meta payloads in, routed queues out. Pure sync enqueue — the HTTP
    handler must return 200 to Meta fast, so nothing here awaits."""

    def __init__(self) -> None:
        self.incoming_calls: asyncio.Queue[CallEvent] = asyncio.Queue(maxsize=8)
        self.chat_texts: asyncio.Queue[TextMessage] = asyncio.Queue(maxsize=32)
        self.chat_buttons: asyncio.Queue[ButtonReply] = asyncio.Queue(maxsize=16)
        self._sessions: dict[str, CallSession] = {}

    # -- session lifecycle ---------------------------------------------------

    def open_call(self, peer: str, direction: Literal["inbound", "outbound"]) -> CallSession:
        peer = peer.lstrip("+")
        session = CallSession(peer=peer, direction=direction)
        self._sessions[peer] = session
        return session

    def close_call(self, session: CallSession) -> None:
        self._sessions.pop(session.peer, None)

    def _session_for(self, call_id: str = "", peer: str = "") -> CallSession | None:
        """Match a CALL event to a session. Sole-active fallback exists
        because Meta omits ids on some status shapes."""
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
        NEVER fall back to the sole active session — doing so kills the live
        call. Returns None until the session's call_id is known (the 90s
        accepted-wait timeout covers a call that terminates before connect)."""
        if not call_id:
            return None
        for session in self._sessions.values():
            if session.call_id and session.call_id == call_id:
                return session
        return None

    def _session_for_peer(self, peer: str) -> CallSession | None:
        """Match a MESSAGE to a session: strict sender match only — another
        peer's texts must never be claimed by someone else's call."""
        return self._sessions.get(peer.lstrip("+")) if peer else None

    # -- routing (parsing preserved from the original webhook receiver) --------

    def dispatch(self, payload: dict[str, Any]) -> None:
        logger.info("webhook: %s", json.dumps(payload)[:800])
        for entry in payload.get("entry", []):
            for change in entry.get("changes", []):
                self._route(change.get("field", ""), change.get("value", {}) or {})

    def _route(self, field_name: str, value: dict[str, Any]) -> None:
        for call in value.get("calls", []) or []:
            self._route_call(call)
        for message in value.get("messages", []) or []:
            self._route_message(message)
        # Some payloads deliver call status under `statuses` instead.
        for status in value.get("statuses", []) or []:
            if str(status.get("type", "")).lower() == "call" or "call" in str(status)[:200].lower():
                state = str(status.get("status", "")).lower()
                status_id = str(status.get("id", ""))
                if state == "accepted" and (
                    session := self._session_for(call_id=status_id)
                ) is not None:
                    session.accepted.set()
                elif state in ("terminated", "failed", "rejected", "ended", "completed") and (
                    session := self._session_by_call_id(status_id)
                ) is not None:
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
            # OFFER = a user is calling US (inbound); ANSWER = reply to our
            # outbound offer.
            if payload.sdp_type == "offer" or payload.direction == "USER_INITIATED":
                _put(self.incoming_calls, payload)
            elif (session := self._session_for(call_id, payload.from_number)) is not None:
                session.call_id = session.call_id or call_id
                _put(session.answers, payload)
            else:
                logger.warning("SDP answer with no open call session; dropped")
        if event in ("terminate", "terminated", "failed", "rejected", "ended") and (
            session := self._session_by_call_id(call_id)
        ) is not None:
            session.ended.set()

    def _route_message(self, message: dict[str, Any]) -> None:
        mtype = message.get("type")
        peer = message.get("from", "")
        session = self._session_for_peer(peer)
        if mtype == "text":
            item = TextMessage(from_number=peer, text=(message.get("text") or {}).get("body", ""))
            _put(session.texts if session else self.chat_texts, item)
            return
        if mtype == "interactive":
            interactive = message.get("interactive") or {}
            reply = interactive.get("call_permission_reply") or {}
            if reply or interactive.get("type") == "call_permission_reply":
                verdict = str(reply.get("response", "")).lower() in ("accept", "accepted")
                if session is not None:
                    _put(session.permission, verdict)
                else:
                    logger.warning("permission reply with no open call session; dropped")
                return
            if interactive.get("type") == "button_reply":
                br = interactive.get("button_reply") or {}
                item = ButtonReply(
                    from_number=peer,
                    button_id=str(br.get("id", "")),
                    title=str(br.get("title", "")),
                )
                _put(session.buttons if session else self.chat_buttons, item)
                return
        # Unknown interactive/button shapes: look for the permission verdict
        # anywhere in the message (button payloads use ACCEPTED/REJECTED).
        blob = json.dumps(message).lower()
        if ("call_permission" in blob or "voice_call_request" in blob) and session is not None:
            _put(session.permission, "accept" in blob)


def _put(queue: asyncio.Queue[Any], item: Any) -> None:
    try:
        queue.put_nowait(item)
    except asyncio.QueueFull:
        logger.warning("event queue full; dropping %r", item)


class TextBridge:
    """Chat<->call sync during a live call: every text the peer sends while
    on a call is (a) injected into the live voice session, (b) checked for
    an email address to satisfy request_email_over_whatsapp, and (c)
    persisted to the shared transcript thread."""

    def __init__(self, session: CallSession, store: Any = None) -> None:
        self._session = session
        self._store = store
        self._inject: Any = None  # async fn(text) -> None, set via attach()
        self._task: asyncio.Task | None = None
        self._email: str | None = None
        self._email_event = asyncio.Event()

    def attach(self, inject_cb: Any) -> None:
        self._inject = inject_cb

    def start(self) -> None:
        self._task = asyncio.get_running_loop().create_task(self._loop())

    async def _loop(self) -> None:
        while True:
            try:
                msg = await self._session.wait_text(timeout_s=3600)
            except TimeoutError:
                continue
            text = msg.text.strip()
            if not text:
                continue
            if match := _EMAIL_RE.search(text):
                self._email = match.group()
                self._email_event.set()
            if self._store is not None:
                await self._store.save("user", f"[via chat] {text}")
            if self._inject is not None:
                try:
                    await self._inject(text)
                except Exception:
                    logger.exception("mid-call text injection failed")

    async def wait_email(self, timeout_s: float) -> str | None:
        try:
            await asyncio.wait_for(self._email_event.wait(), timeout_s)
        except TimeoutError:
            return None
        self._email_event.clear()
        return self._email

    def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
