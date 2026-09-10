"""Compile seam: config -> livekit session kwargs, tool wrapping, chat seeding."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

pytest.importorskip("livekit.agents")

from livekit.agents.llm import ToolError

from voiceagent import SessionMetadata, VoiceAgent, tool
from voiceagent.events import SessionIDs, ToolCallFinished, ToolCallStarted
from voiceagent.livekit import compile as compile_mod
from voiceagent.livekit.compile import (
    SessionRuntime,
    build_chat_context,
    build_session,
    wrap_tool,
)
from voiceagent.tools import ToolContext, ToolFailure

IDS = SessionIDs(agent_id="a", session_id="s")


class RecordingAgentSession:
    def __init__(self, **kwargs: Any) -> None:
        self.kwargs = kwargs


@pytest.fixture
def capture_session(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(compile_mod, "AgentSession", RecordingAgentSession)


def _pipeline_agent(**kw: Any) -> VoiceAgent:
    return VoiceAgent(
        name="p", llm="mock", stt="mock", tts="mock", system_prompt="Hi {name}.", **kw
    )


def test_pipeline_branch_fills_all_slots(capture_session) -> None:
    va = _pipeline_agent()
    _, session = build_session(
        va, SessionMetadata(prompt_vars={"name": "Sam"}), SessionRuntime(ids=IDS), vad="fake-vad"
    )
    assert set(session.kwargs) >= {"stt", "llm", "tts", "vad", "turn_handling", "userdata"}
    assert session.kwargs["vad"] == "fake-vad"
    assert session.kwargs["turn_handling"]["turn_detection"] == "vad"
    assert session.kwargs["turn_handling"]["interruption"] == {"enabled": True}


def test_realtime_branch_puts_model_in_llm_slot(capture_session, monkeypatch) -> None:
    va = VoiceAgent(name="r", mode="realtime", llm="google", system_prompt="x")
    sentinel = object()

    from voiceagent.registry import ProviderRegistry

    reg = ProviderRegistry()
    reg.register("realtime", "google", lambda spec: sentinel)
    monkeypatch.setattr(compile_mod, "ensure_builtins", lambda r=None: reg)

    _, session = build_session(va, SessionMetadata(), SessionRuntime(ids=IDS))
    assert session.kwargs["llm"] is sentinel
    assert "stt" not in session.kwargs and "tts" not in session.kwargs
    assert session.kwargs["turn_handling"]["turn_detection"] == "realtime_llm"


def test_prompt_rendered_from_metadata_vars(capture_session) -> None:
    va = _pipeline_agent()
    agent, _ = build_session(
        va, SessionMetadata(prompt_vars={"name": "Ada"}), SessionRuntime(ids=IDS), vad="v"
    )
    assert agent.instructions == "Hi Ada."


def test_interruptions_disabled_propagates(capture_session) -> None:
    va = _pipeline_agent(allow_interruptions=False)
    _, session = build_session(va, SessionMetadata(prompt_vars={"name": "x"}),
                               SessionRuntime(ids=IDS), vad="v")
    assert session.kwargs["turn_handling"]["interruption"] == {"enabled": False}


def test_build_chat_context_orders_messages() -> None:
    ctx = build_chat_context(
        [
            {"role": "user", "content": "one", "ts": 1.0},
            {"role": "assistant", "content": "two", "ts": 2.0},
        ]
    )
    assert ctx is not None
    texts = [item.text_content for item in ctx.items]
    assert texts == ["one", "two"]
    assert build_chat_context([]) is None


async def test_wrap_tool_success_and_events() -> None:
    events: list = []
    runtime = SessionRuntime(ids=IDS)
    runtime.emit = events.append

    @tool(description="say hi")
    async def hi(ctx: ToolContext, name: str) -> str:
        return f"hi {name}"

    ft = wrap_tool(hi, runtime)
    assert await ft(name="Bob") == "hi Bob"
    kinds = [type(e).__name__ for e in events]
    assert kinds == ["ToolCallStarted", "ToolCallFinished"]
    assert isinstance(events[0], ToolCallStarted) and events[0].arguments == {"name": "Bob"}
    assert isinstance(events[1], ToolCallFinished) and events[1].error is None


async def test_wrap_tool_failure_becomes_spoken_toolerror() -> None:
    runtime = SessionRuntime(ids=IDS)

    @tool(description="fails")
    async def bad(ctx: ToolContext) -> str:
        raise ToolFailure("No slots left today.", detail="calendar 409")

    ft = wrap_tool(bad, runtime)
    with pytest.raises(ToolError, match=r"No slots left today\."):
        await ft()


async def test_wrap_tool_timeout_becomes_toolerror() -> None:
    runtime = SessionRuntime(ids=IDS)

    @tool(description="slow", timeout_s=0.05)
    async def slow(ctx: ToolContext) -> str:
        await asyncio.sleep(1)
        return "never"

    ft = wrap_tool(slow, runtime)
    with pytest.raises(ToolError, match="took too long"):
        await ft()


async def test_wrap_tool_unexpected_error_hides_detail() -> None:
    runtime = SessionRuntime(ids=IDS)

    @tool(description="explodes")
    async def boom(ctx: ToolContext) -> str:
        raise RuntimeError("secret internal path C:/x")

    ft = wrap_tool(boom, runtime)
    with pytest.raises(ToolError) as exc:
        await ft()
    assert "secret" not in str(exc.value.message)
