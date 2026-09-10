"""voiceagent — a reusable voice-agent framework on self-hosted LiveKit.

Core layers:
- ``voiceagent`` (this package root): engine-neutral public API — agents,
  config, prompts, tools, memory, events, provider registry. Zero livekit
  imports so the core installs and imports with no extras.
- ``voiceagent.livekit``: the only place that imports ``livekit*`` — plugin
  factories, the config->AgentSession compile seam, the worker runtime.
- ``voiceagent.platform``: FastAPI control plane (sessions, tokens, SIP calls).
- ``voiceagent.channels``: browser / SIP / WhatsApp channel adapters.
"""

from __future__ import annotations

__version__ = "0.2.0"

__all__ = ["__version__"]
