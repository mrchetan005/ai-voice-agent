"""Canonical session events.

These are the framework's stable event vocabulary. They deliberately contain
no LiveKit, provider, or channel types — channel adapters and the livekit
layer translate their native events into these, so application code written
against them never changes when the engine or a provider does.
"""

from __future__ import annotations

import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True, slots=True)
class SessionIDs:
    """Stable identifiers attached to every event.

    Multi-tenancy is not implemented, but tenant_id is plumbed everywhere so
    adding it later never requires touching the core.
    """

    tenant_id: str = "default"
    agent_id: str = ""
    session_id: str = ""
    room_id: str = ""
    call_id: str | None = None
    user_id: str | None = None
    channel: str = "browser"


@dataclass(frozen=True, slots=True, kw_only=True)
class BaseEvent:
    ids: SessionIDs
    ts: float = field(default_factory=time.time)


@dataclass(frozen=True, slots=True, kw_only=True)
class SessionStarted(BaseEvent):
    pass


@dataclass(frozen=True, slots=True, kw_only=True)
class SessionEnded(BaseEvent):
    reason: str = ""
    duration_s: float = 0.0


@dataclass(frozen=True, slots=True, kw_only=True)
class UserTranscript(BaseEvent):
    text: str
    final: bool = True


@dataclass(frozen=True, slots=True, kw_only=True)
class AgentReply(BaseEvent):
    text: str
    interrupted: bool = False


@dataclass(frozen=True, slots=True, kw_only=True)
class UserStateChanged(BaseEvent):
    state: str  # "speaking" | "listening" | "away"


@dataclass(frozen=True, slots=True, kw_only=True)
class AgentStateChanged(BaseEvent):
    state: str  # "initializing" | "listening" | "thinking" | "speaking"


@dataclass(frozen=True, slots=True, kw_only=True)
class ToolCallStarted(BaseEvent):
    tool: str
    arguments: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True, kw_only=True)
class ToolCallFinished(BaseEvent):
    tool: str
    result: str | None = None
    error: str | None = None
    duration_ms: float = 0.0


@dataclass(frozen=True, slots=True, kw_only=True)
class UsageUpdated(BaseEvent):
    """Normalized usage snapshot: {provider: {unit: amount}}."""

    usage: dict[str, dict[str, float]] = field(default_factory=dict)


@dataclass(frozen=True, slots=True, kw_only=True)
class ErrorEvent(BaseEvent):
    message: str
    recoverable: bool = True


EventHandler = Callable[[BaseEvent], Awaitable[None] | None]
