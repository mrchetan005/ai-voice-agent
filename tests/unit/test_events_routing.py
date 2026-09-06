"""OFFLINE routing checks for EventRouter + CallSession (no keys, no HTTP).

Run:  uv run tests/unit/test_events_routing.py

The load-bearing invariant: while a peer is ON A CALL their texts/buttons/
permission taps go to that call's session; otherwise texts/buttons flow to
the chat lanes and permission taps are dropped.
"""

from __future__ import annotations

import asyncio
import sys

from whatsapp_agent.channels.events import EventRouter

PEER = "919999999999"


def msg_payload(*messages: dict) -> dict:
    return {"entry": [{"changes": [{"field": "messages", "value": {"messages": list(messages)}}]}]}


def text(body: str, sender: str = PEER) -> dict:
    return {"type": "text", "from": sender, "text": {"body": body}}


def button(bid: str, title: str, sender: str = PEER) -> dict:
    return {"type": "interactive", "from": sender,
            "interactive": {"type": "button_reply",
                            "button_reply": {"id": bid, "title": title}}}


def permission(response: str, sender: str = PEER) -> dict:
    return {"type": "interactive", "from": sender,
            "interactive": {"type": "call_permission_reply",
                            "call_permission_reply": {"response": response}}}


async def main() -> int:
    failures: list[str] = []

    def check(name: str, ok: bool) -> None:
        print(f"  [{'PASS' if ok else 'FAIL'}] {name}")
        if not ok:
            failures.append(name)

    router = EventRouter()

    # -- no call open: chat lanes ---------------------------------------------
    router.dispatch(msg_payload(text("hello, my email is a@b.com")))
    got = router.chat_texts.get_nowait()
    check("text -> chat lane", got.from_number == PEER and "a@b.com" in got.text)

    router.dispatch(msg_payload(button("email_ok:a@b.com", "Confirm")))
    tap = router.chat_buttons.get_nowait()
    check("button -> chat lane", tap.button_id == "email_ok:a@b.com")

    router.dispatch(msg_payload(permission("accept")))
    check("permission with no call session is dropped, not misrouted",
          router.chat_texts.empty() and router.chat_buttons.empty())

    # -- call open: the peer's traffic is claimed by the session -----------------
    session = router.open_call(PEER, "outbound")
    router.dispatch(msg_payload(permission("accept")))
    check("permission -> session", session.permission.get_nowait() is True)

    router.dispatch(msg_payload(text("mid-call note")))
    check("text -> session while on call",
          session.texts.get_nowait().text == "mid-call note"
          and router.chat_texts.empty())

    router.dispatch(msg_payload(button("email_ok:c@x.com", "Confirm")))
    check("button -> session while on call",
          session.buttons.get_nowait().button_id == "email_ok:c@x.com"
          and router.chat_buttons.empty())

    other = "918888888888"
    router.dispatch(msg_payload(text("hi", sender=other)))
    check("OTHER peers still reach chat while a call is live",
          router.chat_texts.get_nowait().from_number == other)

    # -- call events -----------------------------------------------------------
    router.dispatch({"entry": [{"changes": [{"field": "calls", "value": {"calls": [
        {"id": "call-1", "session": {"sdp": "v=0...", "sdp_type": "answer"},
         "from": PEER, "direction": "BUSINESS_INITIATED"},
    ]}}]}]})
    answer = session.answers.get_nowait()
    check("SDP answer -> session (call_id learned)",
          answer.call_id == "call-1" and session.call_id == "call-1")

    router.dispatch({"entry": [{"changes": [{"field": "calls", "value": {"statuses": [
        {"type": "call", "id": "call-1", "status": "ACCEPTED"},
    ]}}]}]})
    check("status ACCEPTED -> session.accepted", session.accepted.is_set())

    # A stale/duplicate terminate for a PREVIOUS call (different call_id, and
    # from=business number on BUSINESS_INITIATED) must NOT end the live call.
    # This is the cross-call cutoff: Meta redelivers an old terminate late.
    router.dispatch({"entry": [{"changes": [{"field": "calls", "value": {"calls": [
        {"id": "call-0", "event": "terminate", "from": "15551927186",
         "direction": "BUSINESS_INITIATED"},
    ]}}]}]})
    check("stale terminate for a DIFFERENT call_id does NOT end the live call",
          not session.ended.is_set())

    router.dispatch({"entry": [{"changes": [{"field": "calls", "value": {"statuses": [
        {"type": "call", "id": "call-0", "status": "TERMINATED"},
    ]}}]}]})
    check("stale TERMINATED status for a different call_id does NOT end the call",
          not session.ended.is_set())

    router.dispatch({"entry": [{"changes": [{"field": "calls", "value": {"calls": [
        {"id": "call-1", "event": "terminate"},
    ]}}]}]})
    check("terminate for the MATCHING call_id -> session.ended", session.ended.is_set())

    waiter = asyncio.create_task(session.wait_button(timeout_s=2.0))
    await asyncio.sleep(0.05)
    router.dispatch(msg_payload(button("email_edit", "Edit")))
    check("session.wait_button resolves on arrival",
          (await waiter).button_id == "email_edit")

    # -- close: traffic returns to chat ------------------------------------------
    router.close_call(session)
    router.dispatch(msg_payload(text("back to chat")))
    check("after close_call texts flow to chat again",
          router.chat_texts.get_nowait().text == "back to chat")

    # -- inbound offer ------------------------------------------------------------
    router.dispatch({"entry": [{"changes": [{"field": "calls", "value": {"calls": [
        {"id": "call-2", "session": {"sdp": "v=0...", "sdp_type": "offer"},
         "from": PEER, "direction": "USER_INITIATED"},
    ]}}]}]})
    check("inbound SDP offer -> incoming_calls",
          router.incoming_calls.get_nowait().call_id == "call-2")

    print(f"\n{'ALL PASS' if not failures else f'{len(failures)} FAILURE(S): {failures}'}")
    return 0 if not failures else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))


# -- pytest adapter -----------------------------------------------------------
def test_suite() -> None:
    assert asyncio.run(main()) == 0
