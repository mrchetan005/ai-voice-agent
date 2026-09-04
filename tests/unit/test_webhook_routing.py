"""OFFLINE routing check for WebhookHub (no keys, no network).

Run:  uv run tests/test_webhook_routing.py

Feeds synthetic Meta webhook payloads into hub._route and asserts each
lands in the right queue — texts, reply-button taps (email confirmation),
and call-permission replies must never cross wires.
"""

from __future__ import annotations

import asyncio
import sys

from whatsapp_agent.channels.events import WebhookHub


async def main() -> int:
    failures: list[str] = []

    def check(name: str, ok: bool) -> None:
        print(f"  [{'PASS' if ok else 'FAIL'}] {name}")
        if not ok:
            failures.append(name)

    hub = WebhookHub(verify_token="x")  # not started; queues only

    hub._route("messages", {"messages": [{
        "type": "text", "from": "919999999999",
        "text": {"body": "hello, my email is a@b.com"},
    }]})
    msg = hub.text_messages.get_nowait()
    check("text -> text_messages queue",
          msg.from_number == "919999999999" and "a@b.com" in msg.text)

    hub._route("messages", {"messages": [{
        "type": "interactive", "from": "919999999999",
        "interactive": {
            "type": "button_reply",
            "button_reply": {"id": "email_ok:a@b.com", "title": "Confirm"},
        },
    }]})
    tap = hub.button_replies.get_nowait()
    check("button_reply -> button_replies queue",
          tap.button_id == "email_ok:a@b.com" and tap.title == "Confirm"
          and tap.from_number == "919999999999")
    check("button_reply did NOT hit permission_results",
          hub.permission_results.empty())

    hub._route("messages", {"messages": [{
        "type": "interactive", "from": "919999999999",
        "interactive": {
            "type": "call_permission_reply",
            "call_permission_reply": {"response": "accept"},
        },
    }]})
    check("call_permission_reply -> permission_results (accept=True)",
          hub.permission_results.get_nowait() is True)
    check("permission reply did NOT hit button_replies",
          hub.button_replies.empty())

    hub._route("messages", {"messages": [{
        "type": "interactive", "from": "919999999999",
        "interactive": {"type": "button_reply",
                        "button_reply": {"id": "email_edit", "title": "Edit"}},
    }]})
    check("edit tap routed with its id",
          hub.button_replies.get_nowait().button_id == "email_edit")

    tap_waiter = asyncio.create_task(hub.wait_button(timeout_s=2.0))
    await asyncio.sleep(0.05)
    hub._route("messages", {"messages": [{
        "type": "interactive", "from": "1",
        "interactive": {"type": "button_reply",
                        "button_reply": {"id": "email_ok:x@y.z", "title": "Confirm"}},
    }]})
    check("wait_button() resolves on arrival",
          (await tap_waiter).button_id == "email_ok:x@y.z")

    print(f"\n{'ALL PASS' if not failures else f'{len(failures)} FAILURE(S): {failures}'}")
    return 0 if not failures else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))


# -- pytest adapter -----------------------------------------------------------
def test_suite() -> None:
    assert asyncio.run(main()) == 0
