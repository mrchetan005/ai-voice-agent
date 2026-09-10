"""Unit tests for voiceagent.tools: @tool, Tool.invoke, ToolFailure, ToolContext."""

from __future__ import annotations

import asyncio
import threading
from typing import Any

import pytest

from voiceagent.events import BaseEvent, SessionIDs
from voiceagent.tools import Tool, ToolContext, ToolFailure, tool


def _ctx() -> ToolContext:
    return ToolContext(ids=SessionIDs())


def test_bare_decorator_returns_tool_with_name_and_docstring() -> None:
    @tool
    def greet(name: str) -> str:
        """Say hello.

        Longer docs the LLM should not see.
        """
        return f"hi {name}"

    assert isinstance(greet, Tool)
    assert greet.name == "greet"
    assert greet.description == "Say hello."


def test_parameterized_decorator_overrides_name_and_description() -> None:
    @tool(name="lookup", description="Find a thing.", timeout_s=1.5)
    def find(query: str) -> str:
        return query

    assert isinstance(find, Tool)
    assert find.name == "lookup"
    assert find.description == "Find a thing."
    assert find.timeout_s == 1.5


def test_missing_description_raises_value_error() -> None:
    def nodoc(x: int) -> int:
        return x

    with pytest.raises(ValueError, match="nodoc"):
        tool(nodoc)


def test_missing_param_annotation_raises_naming_the_param() -> None:
    def bad(x) -> int:  # type: ignore[no-untyped-def]
        """Doc."""
        return x

    with pytest.raises(ValueError, match="'x'"):
        tool(bad)


def test_ctx_param_named_ctx_is_detected_and_excluded() -> None:
    @tool
    def with_ctx(ctx, x: int) -> int:
        """Doc."""
        return x

    assert with_ctx.takes_ctx
    assert list(with_ctx.parameters) == ["x"]
    assert "ctx" not in with_ctx.signature.parameters


def test_ctx_detected_by_runtime_annotation_regardless_of_name() -> None:
    def with_context(context, x: int) -> int:
        """Doc."""
        return x

    # Simulate a defining module without PEP 563: real class in annotations.
    with_context.__annotations__["context"] = ToolContext
    t = tool(with_context)
    assert t.takes_ctx
    assert list(t.parameters) == ["x"]


def test_ctx_annotation_detection_under_pep563() -> None:
    # This test module uses `from __future__ import annotations`, so the
    # annotation is the string "ToolContext" — detection must still work
    # (signatures are resolved with eval_str, string compare as fallback).
    def fn(context: ToolContext, x: int) -> int:
        """Doc."""
        return x

    t = tool(fn)
    assert t.takes_ctx
    assert list(t.parameters) == ["x"]


async def test_invoke_passes_ctx_when_takes_ctx() -> None:
    seen: dict[str, Any] = {}

    @tool
    async def probe(ctx, x: int) -> int:
        """Doc."""
        seen["ctx"] = ctx
        return x + 1

    ctx = _ctx()
    assert await probe.invoke(ctx, x=1) == 2
    assert seen["ctx"] is ctx


async def test_invoke_omits_ctx_when_not_taken() -> None:
    seen: dict[str, Any] = {}

    @tool
    async def plain(x: int) -> int:
        """Doc."""
        seen["x"] = x
        return x * 2

    assert await plain.invoke(_ctx(), x=3) == 6
    assert seen == {"x": 3}


async def test_sync_tool_runs_off_event_loop_thread_and_returns_value() -> None:
    main_ident = threading.get_ident()
    seen: dict[str, int] = {}

    @tool
    def sync_add(a: int, b: int) -> int:
        """Doc."""
        seen["ident"] = threading.get_ident()
        return a + b

    assert await sync_add.invoke(_ctx(), a=2, b=3) == 5
    assert seen["ident"] != main_ident


async def test_async_tool_returns_value() -> None:
    @tool
    async def double(x: int) -> int:
        """Doc."""
        await asyncio.sleep(0)
        return x * 2

    assert await double.invoke(_ctx(), x=21) == 42


async def test_timeout_raises_timeout_error() -> None:
    @tool(timeout_s=0.05)
    async def slow() -> None:
        """Doc."""
        await asyncio.sleep(1)

    with pytest.raises(TimeoutError):
        await slow.invoke(_ctx())


def test_tool_failure_carries_message_and_detail() -> None:
    err = ToolFailure("slot unavailable", detail="db row locked")
    assert str(err) == "slot unavailable"
    assert err.message == "slot unavailable"
    assert err.detail == "db row locked"
    assert ToolFailure("oops").detail is None


def test_tool_context_defaults() -> None:
    a, b = _ctx(), _ctx()
    assert a.userdata == {}
    assert a.userdata is not b.userdata
    assert a.memory is None
    assert callable(a.emit)
    assert a.emit(BaseEvent(ids=a.ids)) is None
