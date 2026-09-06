"""OFFLINE regression: a thread whose last checkpoint is an AIMessage with
unanswered tool_calls (barge-in / timeout) must be healed by the NEXT
respond() call — not crash it.

Regression for the live KeyError 'model' / InvalidUpdateError: healing via
aupdate_state re-ran the create_agent graph's routing and crashed; repairs
are now merged into the next turn's input instead.

Run:  uv run tests/unit/test_brain_heal.py
"""

from __future__ import annotations

import asyncio
import os
import sys

from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage, HumanMessage

from whatsapp_agent.agent.brain import BookingAgent


class FakeToolModel(GenericFakeChatModel):
    """Scripted chat model; create_agent needs bind_tools to exist."""

    def bind_tools(self, tools, **kwargs):
        return self


async def main() -> int:
    failures: list[str] = []

    def check(name: str, ok: bool) -> None:
        print(f"  [{'PASS' if ok else 'FAIL'}] {name}")
        if not ok:
            failures.append(name)

    os.environ.setdefault("GOOGLE_API_KEY", "fake-for-offline-test")
    # cal/wa/service are only captured by tool closures — never called here.
    agent = BookingAgent(None, 1, None, "919", None, db_url="postgres://invalid/x")
    agent._llm = FakeToolModel(messages=iter([AIMessage("recovered reply")] * 4))
    await agent.start()
    try:
        thread = "wa-heal-test"
        config = {"configurable": {"thread_id": thread}}
        agent._seeded.add(thread)  # skip DB seeding; we craft the state below

        # Brick the thread: last checkpoint message has unanswered tool_calls.
        await agent._agent.aupdate_state(config, {"messages": [
            HumanMessage("any slots tomorrow?"),
            AIMessage("", tool_calls=[
                {"name": "get_available_slots", "args": {"start_date": "2026-09-08",
                                                         "end_date": "2026-09-08"}, "id": "call1"},
            ]),
        ]})

        repairs = await agent._heal_dangling_tool_calls(config)
        check("heal returns one ToolMessage for the dangling call",
              len(repairs) == 1 and repairs[0].tool_call_id == "call1")

        reply = await agent.respond("actually just book 10am", thread)
        check("respond() on a bricked thread recovers with a reply",
              reply == "recovered reply")

        state = await agent._agent.aget_state(config)
        messages = state.values["messages"]
        answered = any(
            getattr(m, "tool_call_id", None) == "call1" for m in messages
        )
        last = messages[-1]
        check("checkpoint repaired: tool call answered, no dangling tail",
              answered and not (getattr(last, "tool_calls", None) or []))

        reply2 = await agent.respond("and confirm it", thread)
        check("healthy thread: next turn needs no repairs and still works",
              reply2 == "recovered reply"
              and await agent._heal_dangling_tool_calls(config) == [])
    finally:
        await agent.stop()

    print(f"\n{'ALL PASS' if not failures else f'{len(failures)} FAILURE(S): {failures}'}")
    return 0 if not failures else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))


# -- pytest adapter -----------------------------------------------------------
def test_suite() -> None:
    assert asyncio.run(main()) == 0
