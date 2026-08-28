"""Adapters that normalize ANY backend orchestrator to the AgentHandler
contract (``handler(ctx) -> str | AsyncIterator[str] | None``).

The point: plugging your existing agent is one line, whatever it is::

    VoiceAgent(agent=my_async_function, ...)                      # plain python
    VoiceAgent(agent=from_langchain(my_runnable), ...)            # LangChain/LangGraph
    VoiceAgent(agent=from_crewai(my_crew), ...)                   # CrewAI
    VoiceAgent(agent=from_websocket("ws://orch:9000"), ...)       # your own service
    VoiceAgent(agent=from_http("https://orch/run"), ...)          # SSE endpoint

External services (WS/HTTP) speak the ToolStatusPulse JSON contract:

    -> {"type": "user_utterance", "session_id": "...", "text": "...", "language": "hi-IN"}
    <- {"status": "processing", "step": "fetching_db_records"}           # spoken live
    <- {"status": "awaiting_approval", "step": "delete_records",
        "requires_approval": true, "approval_payload": {...}}            # blocks for voice HITL
    -> {"type": "approval_result", "request_id": "...", "approved": true, ...}
    <- {"type": "say", "text": "..."}                                    # immediate speech
    <- {"type": "final", "text": "..."}                                  # closes the turn

Framework imports are lazy: none of these adapters require their framework
installed until actually called.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import logging
from collections.abc import AsyncIterator, Callable
from typing import TYPE_CHECKING, Any

import httpx

from .models import ToolStatusPulse

if TYPE_CHECKING:
    from .commentary_and_approval import AgentContext, AgentHandler

logger = logging.getLogger("voiceagent")


def from_callable(fn: Callable[..., Any]) -> AgentHandler:
    """Validate and pass through a plain function/coroutine/async-generator.

    Exists so the facade can give a clear error at construction time instead
    of a confusing one mid-conversation.
    """
    if not callable(fn):
        raise TypeError(f"agent must be callable, got {type(fn)!r}")
    sig = inspect.signature(fn)
    if len(sig.parameters) != 1:
        raise TypeError(
            "agent handler must take exactly one argument (ctx: AgentContext); "
            f"got signature {sig}"
        )
    return fn


# ---------------------------------------------------------------------------
# LangChain / LangGraph
# ---------------------------------------------------------------------------


def from_langchain(runnable: Any, input_key: str = "input") -> AgentHandler:
    """Mount a LangChain Runnable / compiled LangGraph as the agent.

    Streams via ``astream_events`` v2: chat-model token chunks become spoken
    text deltas; ``on_tool_start`` events become live commentary pulses —
    the user hears "I'm searching the web" the moment the graph enters a
    tool node, not after the whole run.
    """
    if not hasattr(runnable, "astream_events"):
        raise TypeError(
            "from_langchain expects a Runnable with .astream_events "
            "(langchain-core >= 0.3); install the 'langchain' extra."
        )

    def handler(ctx: AgentContext) -> AsyncIterator[str]:
        async def gen() -> AsyncIterator[str]:
            payload = {input_key: ctx.text, "history": ctx.history}
            async for event in runnable.astream_events(payload, version="v2"):
                kind = event.get("event")
                if kind == "on_chat_model_stream":
                    chunk = event.get("data", {}).get("chunk")
                    text = getattr(chunk, "content", None)
                    if isinstance(text, str) and text:
                        yield text
                elif kind == "on_tool_start":
                    await ctx.status(event.get("name") or "running_a_tool")
                elif kind == "on_tool_end":
                    # Deliberately silent: completion pulses for every tool
                    # double the chatter without adding information.
                    continue

        return gen()

    return handler


# ---------------------------------------------------------------------------
# CrewAI
# ---------------------------------------------------------------------------


def from_crewai(crew: Any, input_key: str = "query") -> AgentHandler:
    """Mount a CrewAI crew. ``kickoff`` is synchronous, so it runs in a
    worker thread; step callbacks hop back onto the event loop to emit
    commentary pulses in real time."""
    if not hasattr(crew, "kickoff"):
        raise TypeError("from_crewai expects an object with .kickoff (a Crew)")

    async def handler(ctx: AgentContext) -> str:
        loop = asyncio.get_running_loop()

        def step_callback(step: Any) -> None:  # runs on the worker thread
            name = getattr(step, "tool", None) or type(step).__name__
            # Fire-and-forget: commentary must never block crew execution.
            asyncio.run_coroutine_threadsafe(ctx.status(str(name)), loop)

        previous = getattr(crew, "step_callback", None)
        crew.step_callback = step_callback
        try:
            result = await asyncio.to_thread(crew.kickoff, inputs={input_key: ctx.text})
        finally:
            crew.step_callback = previous
        return str(result)

    return handler


# ---------------------------------------------------------------------------
# External WebSocket orchestrator
# ---------------------------------------------------------------------------


def from_websocket(url: str, connect_timeout_s: float = 10.0) -> AgentHandler:
    """Bridge to any service speaking the ToolStatusPulse JSON contract
    over a WebSocket (one connection per turn keeps the contract stateless;
    the orchestrator correlates turns via ``session_id``)."""

    def handler(ctx: AgentContext) -> AsyncIterator[str]:
        async def gen() -> AsyncIterator[str]:
            import websockets  # local import: adapter stays importable anywhere

            async with asyncio.timeout(connect_timeout_s):
                ws = await websockets.connect(url)
            try:
                await ws.send(json.dumps({
                    "type": "user_utterance",
                    "session_id": ctx.config.session_id,
                    "text": ctx.text,
                    "language": ctx.config.language,
                }))
                async for raw in ws:
                    msg = json.loads(raw)
                    mtype = msg.get("type")
                    if mtype == "final":
                        if text := msg.get("text"):
                            yield text
                        return
                    if mtype == "say":
                        await ctx.say(msg.get("text", ""))
                        continue
                    if "step" in msg:
                        pulse = ToolStatusPulse.model_validate(msg)
                        result = await ctx.emit(pulse)
                        if result is not None:
                            # HITL verdict goes straight back upstream so the
                            # orchestrator can resume or abort its workflow.
                            await ws.send(json.dumps(result.to_upstream()))
            finally:
                await ws.close()

        return gen()

    return handler


# ---------------------------------------------------------------------------
# External HTTP (SSE) orchestrator
# ---------------------------------------------------------------------------


def from_http(url: str, approval_path: str = "/approval") -> AgentHandler:
    """Bridge to an HTTP orchestrator that streams SSE ``data:`` lines with
    the same JSON contract; approval verdicts POST to ``url + approval_path``.
    """

    def handler(ctx: AgentContext) -> AsyncIterator[str]:
        async def gen() -> AsyncIterator[str]:
            async with (
                httpx.AsyncClient(timeout=httpx.Timeout(None, connect=10.0)) as client,
                client.stream("POST", url, json={
                    "type": "user_utterance",
                    "session_id": ctx.config.session_id,
                    "text": ctx.text,
                    "language": ctx.config.language,
                }) as response,
            ):
                response.raise_for_status()
                async for line in response.aiter_lines():
                    if not line.startswith("data:"):
                        continue
                    msg = json.loads(line[5:].strip())
                    mtype = msg.get("type")
                    if mtype == "final":
                        if text := msg.get("text"):
                            yield text
                        return
                    if mtype == "say":
                        await ctx.say(msg.get("text", ""))
                        continue
                    if "step" in msg:
                        pulse = ToolStatusPulse.model_validate(msg)
                        result = await ctx.emit(pulse)
                        if result is not None:
                            await client.post(
                                url.rstrip("/") + approval_path,
                                json=result.to_upstream(),
                            )

        return gen()

    return handler
