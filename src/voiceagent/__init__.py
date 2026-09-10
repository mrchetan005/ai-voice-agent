"""voiceagent — a reusable voice-agent framework on self-hosted LiveKit.

Layers:
- ``voiceagent`` (this package root): engine-neutral public API — agents,
  config, prompts, tools, memory, events, provider registry. Zero livekit
  imports, so the core installs and imports with no extras.
- ``voiceagent.livekit``: the only place that imports ``livekit*`` — plugin
  factories, the config->AgentSession compile seam, the worker runtime.
- ``voiceagent.platform``: FastAPI control plane (sessions, tokens, SIP calls).
- ``voiceagent.channels``: browser / SIP / WhatsApp channel adapters.
"""

from __future__ import annotations

from voiceagent.agent import VoiceAgent, run
from voiceagent.config import (
    AgentConfig,
    LLMConfig,
    MemoryConfig,
    RealtimeConfig,
    STTConfig,
    TTSConfig,
    load_agents_yaml,
)
from voiceagent.events import (
    AgentReply,
    AgentStateChanged,
    BaseEvent,
    ErrorEvent,
    EventHandler,
    SessionEnded,
    SessionIDs,
    SessionStarted,
    ToolCallFinished,
    ToolCallStarted,
    UsageUpdated,
    UserStateChanged,
    UserTranscript,
)
from voiceagent.memory import Memory, Message, memory_from_config
from voiceagent.metadata import SessionMetadata
from voiceagent.prompts import PromptConfig, PromptError, PromptTemplate
from voiceagent.registry import (
    ProviderRegistry,
    ProviderSpec,
    UnknownProviderError,
    register_llm,
    register_realtime,
    register_stt,
    register_tts,
    register_vad,
    registry,
)
from voiceagent.tools import Tool, ToolContext, ToolFailure, tool

__version__ = "0.2.0"

__all__ = [
    "AgentConfig",
    "AgentReply",
    "AgentStateChanged",
    "BaseEvent",
    "ErrorEvent",
    "EventHandler",
    "LLMConfig",
    "Memory",
    "MemoryConfig",
    "Message",
    "PromptConfig",
    "PromptError",
    "PromptTemplate",
    "ProviderRegistry",
    "ProviderSpec",
    "RealtimeConfig",
    "STTConfig",
    "SessionEnded",
    "SessionIDs",
    "SessionMetadata",
    "SessionStarted",
    "TTSConfig",
    "Tool",
    "ToolCallFinished",
    "ToolCallStarted",
    "ToolContext",
    "ToolFailure",
    "UnknownProviderError",
    "UsageUpdated",
    "UserStateChanged",
    "UserTranscript",
    "VoiceAgent",
    "__version__",
    "load_agents_yaml",
    "memory_from_config",
    "register_llm",
    "register_realtime",
    "register_stt",
    "register_tts",
    "register_vad",
    "registry",
    "run",
    "tool",
]
