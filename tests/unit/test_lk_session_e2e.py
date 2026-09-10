"""Headless end-to-end: mock providers through a REAL livekit AgentSession.

Uses the text-eval path (session.run) so no audio devices, rooms, or network
are needed — this proves compile + bridge + tools against the real engine.
"""

from __future__ import annotations

import asyncio

import pytest

pytest.importorskip("livekit.agents")

from voiceagent import SessionMetadata, VoiceAgent, tool
from voiceagent.events import AgentReply, SessionIDs, UsageUpdated
from voiceagent.livekit.bridge import attach_bridge, make_dispatcher
from voiceagent.livekit.compile import SessionRuntime, build_session
from voiceagent.memory.inmemory import InMemoryMemory
from voiceagent.tools import ToolContext

IDS = SessionIDs(agent_id="mock-demo", session_id="e2e")


async def _run_turn(va: VoiceAgent, text: str, runtime: SessionRuntime) -> list:
    events: list = []
    runtime.emit = make_dispatcher([events.append])
    lk_agent, session = build_session(va, SessionMetadata(), runtime, vad=None)
    attach_bridge(session, runtime)
    await session.start(lk_agent)
    try:
        await session.run(user_input=text)
        await asyncio.sleep(0.1)  # let fire-and-forget handlers settle
    finally:
        await session.aclose()
    return events


async def test_echo_turn_produces_reply_and_usage() -> None:
    va = VoiceAgent(name="mock-demo", llm="mock", stt="mock", tts="mock", system_prompt="Echo.")
    events = await _run_turn(va, "hello there", SessionRuntime(ids=IDS))

    replies = [e for e in events if isinstance(e, AgentReply)]
    assert replies and replies[0].text == "You said: hello there."
    usage = [e for e in events if isinstance(e, UsageUpdated)]
    assert usage and "mock" in usage[-1].usage


async def test_memory_captures_both_sides_of_turn() -> None:
    va = VoiceAgent(name="mock-demo", llm="mock", stt="mock", tts="mock", system_prompt="Echo.")
    memory = InMemoryMemory()
    runtime = SessionRuntime(ids=IDS, memory=memory, memory_key="caller-1")
    await _run_turn(va, "remember me", runtime)

    messages = await memory.load("caller-1")
    contents = [(m["role"], m["content"]) for m in messages]
    assert ("user", "remember me") in contents
    assert any(role == "assistant" and "remember me" in text for role, text in contents)


async def test_seeded_history_reaches_the_llm() -> None:
    va = VoiceAgent(name="mock-demo", llm="mock", stt="mock", tts="mock", system_prompt="Echo.")
    runtime = SessionRuntime(ids=IDS)
    events: list = []
    runtime.emit = make_dispatcher([events.append])
    lk_agent, session = build_session(
        va,
        SessionMetadata(),
        runtime,
        vad=None,
        history=[{"role": "user", "content": "I am Ada", "ts": 1.0}],
    )
    assert [i.text_content for i in lk_agent.chat_ctx.items] == ["I am Ada"]
    await session.start(lk_agent)
    await session.aclose()


async def test_tool_call_path_with_real_session() -> None:
    calls: list[str] = []

    @tool(description="Record a value")
    async def record(ctx: ToolContext, value: str) -> str:
        calls.append(value)
        return f"recorded {value}"

    va = VoiceAgent(
        name="mock-demo", llm="mock", stt="mock", tts="mock", system_prompt="Echo.",
        tools=[record],
    )
    runtime = SessionRuntime(ids=IDS)
    lk_agent, session = build_session(va, SessionMetadata(), runtime, vad=None)
    # MockLLM never calls tools; assert the wrapped tool is registered and callable.
    assert [t.id for t in lk_agent.tools] == ["record"]
    result = await lk_agent.tools[0](value="x")
    assert result == "recorded x" and calls == ["x"]
    await session.start(lk_agent)
    await session.aclose()
