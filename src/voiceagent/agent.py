"""VoiceAgent: the flagship public API.

Sugar over :class:`voiceagent.config.AgentConfig` — flat keyword arguments for
the common case, a canonical config underneath, and event/tool registration.
Running an agent lives in the livekit layer; ``run()`` imports it lazily so
the core package works without the ``agents`` extra installed.
"""

from __future__ import annotations

import importlib
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any, Literal

from voiceagent.config import (
    AgentConfig,
    LLMConfig,
    MemoryConfig,
    RealtimeConfig,
    STTConfig,
    TTSConfig,
)
from voiceagent.events import EventHandler
from voiceagent.prompts import PromptConfig, PromptTemplate
from voiceagent.tools import Tool
from voiceagent.tools import tool as make_tool


def _split_provider(value: str) -> tuple[str, str | None]:
    """"deepgram:nova-3" -> ("deepgram", "nova-3"); "deepgram" -> ("deepgram", None)."""
    name, _, model = value.partition(":")
    return name, model or None


def _normalize_prompt(prompt: str | PromptTemplate | PromptConfig | None) -> PromptConfig:
    if prompt is None:
        raise ValueError("an agent needs a system_prompt (or a prompt in its config)")
    if isinstance(prompt, PromptConfig):
        return prompt
    if isinstance(prompt, PromptTemplate):
        return PromptConfig(text=prompt.template, variables=prompt.defaults)
    return PromptConfig(text=prompt)


def _normalize_memory(memory: MemoryConfig | str | None) -> MemoryConfig | None:
    if memory is None or isinstance(memory, MemoryConfig):
        return memory
    return MemoryConfig(backend=memory)  # type: ignore[arg-type]


def resolve_tool_path(path: str) -> Tool:
    """Import a Tool from a dotted path like ``pkg.module:tool_name``."""
    module_path, _, attr = path.partition(":")
    if not attr:
        raise ValueError(f"tool path '{path}' must look like 'package.module:attribute'")
    obj = getattr(importlib.import_module(module_path), attr)
    if isinstance(obj, Tool):
        return obj
    if callable(obj):
        return make_tool(obj)
    raise TypeError(f"tool path '{path}' resolved to {type(obj).__name__}, not a Tool/callable")


class VoiceAgent:
    def __init__(
        self,
        name: str,
        *,
        mode: Literal["pipeline", "realtime"] = "pipeline",
        llm: str | None = None,
        model: str | None = None,
        stt: str | None = None,
        tts: str | None = None,
        system_prompt: str | PromptTemplate | PromptConfig | None = None,
        tools: Sequence[Tool | Callable[..., Any]] = (),
        language: str = "en",
        voice: str | None = None,
        temperature: float | None = None,
        greeting: str | None = None,
        memory: MemoryConfig | str | None = None,
        turn_detection: Literal["vad", "multilingual"] = "vad",
        allow_interruptions: bool = True,
        max_tool_steps: int = 5,
        llm_options: dict[str, Any] | None = None,
        stt_options: dict[str, Any] | None = None,
        tts_options: dict[str, Any] | None = None,
        config: AgentConfig | None = None,
    ) -> None:
        self.tools: list[Tool] = [
            t if isinstance(t, Tool) else make_tool(t) for t in tools
        ]
        self._handlers: list[EventHandler] = []

        if config is not None:
            self.config = config
            self.tools.extend(resolve_tool_path(p) for p in config.tools)
            return

        if mode == "realtime":
            if stt or tts:
                raise ValueError("realtime mode does not take stt/tts (the model is the voice)")
            if llm is None:
                raise ValueError("realtime mode needs llm=<realtime provider> (e.g. 'google')")
            self.config = AgentConfig(
                name=name,
                mode="realtime",
                prompt=_normalize_prompt(system_prompt),
                realtime=RealtimeConfig(
                    provider=llm, model=model, voice=voice, options=llm_options or {}
                ),
                language=language,
                greeting=greeting,
                memory=_normalize_memory(memory),
                turn_detection=turn_detection,
                allow_interruptions=allow_interruptions,
                max_tool_steps=max_tool_steps,
            )
            return

        if llm is None:
            raise ValueError("pipeline mode needs llm=<provider> (e.g. 'google', 'openai')")
        stt_name, stt_model = _split_provider(stt or "deepgram")
        tts_name, tts_model = _split_provider(tts or "cartesia")
        self.config = AgentConfig(
            name=name,
            mode="pipeline",
            prompt=_normalize_prompt(system_prompt),
            llm=LLMConfig(
                provider=llm, model=model, temperature=temperature, options=llm_options or {}
            ),
            stt=STTConfig(
                provider=stt_name, model=stt_model, language=language, options=stt_options or {}
            ),
            tts=TTSConfig(
                provider=tts_name,
                model=tts_model,
                voice=voice,
                language=language,
                options=tts_options or {},
            ),
            language=language,
            greeting=greeting,
            memory=_normalize_memory(memory),
            turn_detection=turn_detection,
            allow_interruptions=allow_interruptions,
            max_tool_steps=max_tool_steps,
        )

    @property
    def name(self) -> str:
        return self.config.name

    @classmethod
    def from_config(cls, source: AgentConfig | dict[str, Any] | str | Path) -> VoiceAgent:
        if isinstance(source, str | Path):
            source = AgentConfig.from_yaml(source)
        elif isinstance(source, dict):
            source = AgentConfig.from_dict(source)
        return cls(source.name, config=source)

    def add_tool(self, t: Tool | Callable[..., Any]) -> Tool:
        resolved = t if isinstance(t, Tool) else make_tool(t)
        self.tools.append(resolved)
        return resolved

    def on_event(self, handler: EventHandler) -> EventHandler:
        """Register a canonical-event handler; usable as a decorator."""
        self._handlers.append(handler)
        return handler

    @property
    def event_handlers(self) -> tuple[EventHandler, ...]:
        return tuple(self._handlers)

    def __repr__(self) -> str:
        return f"VoiceAgent(name={self.name!r}, mode={self.config.mode!r})"


def run(*agents: VoiceAgent) -> None:
    """Boot the worker for these agents (argv: dev / start / console / download-files)."""
    from voiceagent.livekit.worker import run as _run

    _run(*agents)
