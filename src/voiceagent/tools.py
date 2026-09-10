"""Tool system: plain Python functions the agent can call.

Tools are engine-neutral: they receive a :class:`ToolContext` (never a LiveKit
type) and raise :class:`ToolFailure` for errors that should be spoken back to
the user. The livekit layer wraps a :class:`Tool` into the engine's native
function-tool format.
"""

from __future__ import annotations

import asyncio
import inspect
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, overload

from voiceagent.events import BaseEvent, SessionIDs

if TYPE_CHECKING:
    from voiceagent.memory import Memory


class ToolFailure(Exception):
    """A tool error whose message is safe to speak to the user.

    ``detail`` is for logs only and never reaches the model or the user.
    """

    def __init__(self, message: str, *, detail: str | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.detail = detail


@dataclass(slots=True)
class ToolContext:
    """What a tool can see and do, independent of the engine."""

    ids: SessionIDs
    memory: Memory | None = None
    userdata: dict[str, Any] = field(default_factory=dict)
    emit: Callable[[BaseEvent], None] = lambda event: None


def _resolved_signature(fn: Callable[..., Any]) -> inspect.Signature:
    """Signature with annotations evaluated (PEP 563 makes them strings otherwise)."""
    try:
        return inspect.signature(fn, eval_str=True)
    except Exception:  # unresolvable forward refs — fall back to raw strings
        return inspect.signature(fn)


def _is_ctx_param(param: inspect.Parameter) -> bool:
    return (
        param.annotation is ToolContext
        or param.annotation == "ToolContext"
        or param.name == "ctx"
    )


class Tool:
    """A callable tool with metadata the LLM needs to invoke it."""

    def __init__(
        self,
        fn: Callable[..., Any],
        *,
        name: str | None = None,
        description: str | None = None,
        timeout_s: float = 15.0,
    ) -> None:
        self.fn = fn
        self.name = name or fn.__name__
        doc = inspect.getdoc(fn)
        self.description = description or (doc.splitlines()[0] if doc else "")
        if not self.description:
            raise ValueError(f"tool '{self.name}' needs a description (argument or docstring)")
        self.timeout_s = timeout_s

        signature = _resolved_signature(fn)
        params = list(signature.parameters.values())
        self.takes_ctx = bool(params) and _is_ctx_param(params[0])
        if self.takes_ctx:
            params = params[1:]
        for param in params:
            if param.annotation is inspect.Parameter.empty:
                raise ValueError(
                    f"tool '{self.name}': parameter '{param.name}' needs a type annotation "
                    "so its schema can be derived"
                )
        self.parameters: dict[str, inspect.Parameter] = {p.name: p for p in params}
        self.signature = signature.replace(parameters=params)

    async def invoke(self, ctx: ToolContext, **kwargs: Any) -> Any:
        args = (ctx, *()) if self.takes_ctx else ()
        async with asyncio.timeout(self.timeout_s):
            if inspect.iscoroutinefunction(self.fn):
                return await self.fn(*args, **kwargs)
            return await asyncio.to_thread(self.fn, *args, **kwargs)

    def __repr__(self) -> str:
        return f"Tool(name={self.name!r}, params={list(self.parameters)!r})"


@overload
def tool(fn: Callable[..., Any]) -> Tool: ...
@overload
def tool(
    *, name: str | None = ..., description: str | None = ..., timeout_s: float = ...
) -> Callable[[Callable[..., Any]], Tool]: ...


def tool(
    fn: Callable[..., Any] | None = None,
    *,
    name: str | None = None,
    description: str | None = None,
    timeout_s: float = 15.0,
) -> Tool | Callable[[Callable[..., Any]], Tool]:
    """Declare a tool: ``@tool`` or ``@tool(description=..., timeout_s=...)``."""

    def wrap(f: Callable[..., Any]) -> Tool:
        return Tool(f, name=name, description=description, timeout_s=timeout_s)

    return wrap(fn) if fn is not None else wrap
