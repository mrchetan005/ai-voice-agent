"""The compile seam: AgentConfig + SessionMetadata -> livekit Agent + AgentSession.

This module is where the engine-neutral world (voiceagent.core) meets
livekit-agents. Nothing above this layer imports livekit; nothing below it
knows about VoiceAgent.
"""

from __future__ import annotations

import inspect
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from livekit.agents import Agent, AgentSession, TurnHandlingOptions, function_tool
from livekit.agents.llm import ChatContext, ToolError

from voiceagent.agent import VoiceAgent
from voiceagent.config import AgentConfig
from voiceagent.events import BaseEvent, SessionIDs, ToolCallFinished, ToolCallStarted
from voiceagent.livekit.providers import ensure_builtins
from voiceagent.memory import Memory, Message
from voiceagent.metadata import SessionMetadata
from voiceagent.observability.usage import UsageCollector
from voiceagent.registry import ProviderRegistry, ProviderSpec
from voiceagent.tools import Tool, ToolContext, ToolFailure

logger = logging.getLogger("voiceagent")


@dataclass(slots=True)
class SessionRuntime:
    """Per-session state shared between the worker, bridge, and tools."""

    ids: SessionIDs
    memory: Memory | None = None
    memory_key: str | None = None
    emit: Callable[[BaseEvent], None] = lambda event: None
    usage: UsageCollector = field(default_factory=UsageCollector)
    userdata: dict[str, Any] = field(default_factory=dict)


def _spec_for(cfg: Any, language: str) -> ProviderSpec:
    return ProviderSpec(
        model=getattr(cfg, "model", None),
        language=getattr(cfg, "language", None) or language,
        voice=getattr(cfg, "voice", None),
        temperature=getattr(cfg, "temperature", None),
        options=dict(getattr(cfg, "options", {}) or {}),
    )


def wrap_tool(tool: Tool, runtime: SessionRuntime) -> Any:
    """Bridge a voiceagent Tool into a livekit FunctionTool.

    The wrapper exposes the tool's ctx-free signature so livekit derives the
    LLM schema, builds a ToolContext per call, emits canonical tool events,
    and maps ToolFailure/timeout onto spoken-safe ToolError messages.
    """

    async def _invoke(**kwargs: Any) -> Any:
        ctx = ToolContext(
            ids=runtime.ids,
            memory=runtime.memory,
            userdata=runtime.userdata,
            emit=runtime.emit,
        )
        runtime.emit(ToolCallStarted(ids=runtime.ids, tool=tool.name, arguments=dict(kwargs)))
        started = time.monotonic()
        try:
            result = await tool.invoke(ctx, **kwargs)
        except ToolFailure as exc:
            logger.warning("tool %s failed: %s (%s)", tool.name, exc.message, exc.detail)
            runtime.emit(
                ToolCallFinished(
                    ids=runtime.ids,
                    tool=tool.name,
                    error=exc.message,
                    duration_ms=(time.monotonic() - started) * 1000,
                )
            )
            raise ToolError(exc.message) from exc
        except TimeoutError as exc:
            logger.warning("tool %s timed out after %.1fs", tool.name, tool.timeout_s)
            runtime.emit(
                ToolCallFinished(
                    ids=runtime.ids,
                    tool=tool.name,
                    error="timeout",
                    duration_ms=(time.monotonic() - started) * 1000,
                )
            )
            raise ToolError("That took too long — please try again.") from exc
        except Exception as exc:
            logger.exception("tool %s raised", tool.name)
            runtime.emit(
                ToolCallFinished(
                    ids=runtime.ids,
                    tool=tool.name,
                    error=str(exc),
                    duration_ms=(time.monotonic() - started) * 1000,
                )
            )
            raise ToolError("Something went wrong with that request.") from exc
        runtime.emit(
            ToolCallFinished(
                ids=runtime.ids,
                tool=tool.name,
                result=str(result) if result is not None else None,
                duration_ms=(time.monotonic() - started) * 1000,
            )
        )
        return result

    # Expose the ctx-free signature/annotations so schema inference works.
    _invoke.__signature__ = tool.signature  # type: ignore[attr-defined]
    _invoke.__annotations__ = {
        name: param.annotation
        for name, param in tool.parameters.items()
        if param.annotation is not inspect.Parameter.empty
    }
    _invoke.__name__ = tool.name
    return function_tool(_invoke, name=tool.name, description=tool.description)


def build_chat_context(history: list[Message]) -> ChatContext | None:
    if not history:
        return None
    ctx = ChatContext.empty()
    for message in history:
        ctx.add_message(
            role=message["role"], content=message["content"], created_at=message["ts"]
        )
    return ctx


def _turn_handling(cfg: AgentConfig) -> TurnHandlingOptions:
    if cfg.mode == "realtime":
        turn_detection: Any = "realtime_llm"
    elif cfg.turn_detection == "multilingual":
        from livekit.plugins.turn_detector.multilingual import MultilingualModel

        turn_detection = MultilingualModel()
    else:
        turn_detection = "vad"
    return TurnHandlingOptions(
        turn_detection=turn_detection,
        interruption={"enabled": cfg.allow_interruptions},
    )


def build_session(
    va: VoiceAgent,
    meta: SessionMetadata,
    runtime: SessionRuntime,
    *,
    registry: ProviderRegistry | None = None,
    vad: Any = None,
    history: list[Message] | None = None,
) -> tuple[Agent, AgentSession]:
    """Compile a VoiceAgent + dispatch metadata into livekit objects."""
    cfg = va.config
    reg = ensure_builtins(registry)

    prompt_template = cfg.prompt.resolve()
    instructions = prompt_template.render(**meta.prompt_vars)

    language = meta.language or cfg.language
    if cfg.mode == "realtime":
        assert cfg.realtime is not None  # validated by AgentConfig
        session_kwargs: dict[str, Any] = {
            "llm": reg.create("realtime", cfg.realtime.provider, _spec_for(cfg.realtime, language))
        }
    else:
        assert cfg.llm and cfg.stt and cfg.tts  # validated by AgentConfig
        session_kwargs = {
            "stt": reg.create("stt", cfg.stt.provider, _spec_for(cfg.stt, language)),
            "llm": reg.create("llm", cfg.llm.provider, _spec_for(cfg.llm, language)),
            "tts": reg.create("tts", cfg.tts.provider, _spec_for(cfg.tts, language)),
            "vad": vad if vad is not None else reg.create("vad", "silero"),
        }

    session = AgentSession(
        turn_handling=_turn_handling(cfg),
        max_tool_steps=cfg.max_tool_steps,
        userdata=runtime,
        **session_kwargs,
    )

    agent = Agent(
        instructions=instructions,
        tools=[wrap_tool(t, runtime) for t in va.tools],
        chat_ctx=build_chat_context(history or []),
    )
    return agent, session
