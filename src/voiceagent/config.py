"""Agent configuration: the canonical, declarative form of an agent.

Everything the sugar API (:class:`voiceagent.agent.VoiceAgent`) accepts
compiles down to an :class:`AgentConfig`; the livekit layer compiles an
AgentConfig into a running session. YAML/dict definitions land here directly.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, Field, field_validator, model_validator

from voiceagent.prompts import PromptConfig

_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]*$")


class LLMConfig(BaseModel):
    provider: str
    model: str | None = None
    temperature: float | None = None
    options: dict[str, Any] = Field(default_factory=dict)


class STTConfig(BaseModel):
    provider: str = "deepgram"
    model: str | None = None
    language: str = "en"
    options: dict[str, Any] = Field(default_factory=dict)


class TTSConfig(BaseModel):
    provider: str = "cartesia"
    model: str | None = None
    voice: str | None = None
    language: str = "en"
    options: dict[str, Any] = Field(default_factory=dict)


class RealtimeConfig(BaseModel):
    provider: str
    model: str | None = None
    voice: str | None = None
    options: dict[str, Any] = Field(default_factory=dict)


class MemoryConfig(BaseModel):
    backend: Literal["inmemory", "redis", "postgres"] = "inmemory"
    url: str | None = None  # falls back to platform settings when None
    max_messages: int = 50
    ttl_s: int = 604_800


class AgentConfig(BaseModel):
    name: str
    mode: Literal["pipeline", "realtime"] = "pipeline"
    prompt: PromptConfig
    llm: LLMConfig | None = None
    stt: STTConfig | None = None
    tts: TTSConfig | None = None
    realtime: RealtimeConfig | None = None
    tools: list[str] = Field(default_factory=list)  # dotted paths "pkg.mod:tool_obj"
    language: str = "en"
    greeting: str | None = None
    memory: MemoryConfig | None = None
    turn_detection: Literal["vad", "multilingual"] = "vad"
    allow_interruptions: bool = True
    max_tool_steps: int = 5
    metadata: dict[str, str] = Field(default_factory=dict)

    @field_validator("name")
    @classmethod
    def _valid_name(cls, v: str) -> str:
        if not _NAME_RE.match(v):
            raise ValueError(
                "agent name must be lowercase letters/digits/hyphens (it doubles as the "
                "LiveKit agent_name and appears in room names)"
            )
        return v

    @model_validator(mode="after")
    def _mode_consistency(self) -> AgentConfig:
        if self.mode == "pipeline":
            if self.realtime is not None:
                raise ValueError("pipeline mode does not take a 'realtime' config")
            if self.llm is None:
                raise ValueError("pipeline mode requires an 'llm' config")
            if self.stt is None or self.tts is None:
                raise ValueError("pipeline mode requires 'stt' and 'tts' configs")
        else:
            if self.realtime is None:
                raise ValueError("realtime mode requires a 'realtime' config")
            if self.llm or self.stt or self.tts:
                raise ValueError("realtime mode does not take llm/stt/tts configs")
        return self

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> AgentConfig:
        return cls.model_validate(data)

    @classmethod
    def from_yaml(cls, path: str | Path) -> AgentConfig:
        data = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
        if isinstance(data, dict) and "agents" in data:
            raise ValueError("this file defines multiple agents; use load_agents_yaml()")
        return cls.from_dict(data)


def load_agents_yaml(path: str | Path) -> list[AgentConfig]:
    """Load one or many agent definitions from a YAML file.

    Accepts either a single agent mapping or ``{"agents": [ ... ]}``.
    """
    data = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    entries = data["agents"] if isinstance(data, dict) and "agents" in data else [data]
    if not isinstance(entries, list):
        raise TypeError(f"{path}: expected an agent mapping or an 'agents' list")
    configs = [AgentConfig.from_dict(entry) for entry in entries]
    names = [c.name for c in configs]
    if len(set(names)) != len(names):
        raise ValueError(f"{path}: duplicate agent names: {names}")
    return configs
