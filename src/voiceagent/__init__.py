"""voiceagent — plug any orchestration agent into a realtime voice interface.

Minimal use::

    from voiceagent import VoiceAgent

    async def my_agent(ctx):
        await ctx.status("fetching_db_records")
        if not await ctx.approve("Delete 45 records?"):
            return "Okay, cancelled."
        return "Done!"

    VoiceAgent(provider="deepgram+groq+cartesia", agent=my_agent,
               language="hi-IN", tone="warm").run("ws://0.0.0.0:8765")

Providers: ``openai-realtime`` | ``gemini-live`` | ``mock`` | any
``asr+llm+tts`` combo of {deepgram, deepgram-flux} + {groq, openai} +
{cartesia, elevenlabs}.  Transports: ``local`` (mic/speakers),
``ws://host:port`` (server for browser/phone clients), or any object
implementing :class:`voiceagent.base.AudioTransport`.

API keys come from conventional env vars: OPENAI_API_KEY, GOOGLE_API_KEY,
DEEPGRAM_API_KEY, CARTESIA_API_KEY, ELEVENLABS_API_KEY, GROQ_API_KEY.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from typing import Any

from .adapters import (
    from_callable,
    from_crewai,
    from_http,
    from_langchain,
    from_websocket,
)
from .base import (
    AudioTransport,
    BaseVoiceAgentProxy,
    LocalAudioTransport,
    WebSocketTransport,
)
from .commentary_and_approval import AgentContext, AgentHandler, OrchestratorBridge
from .models import (
    ApprovalRequest,
    ApprovalResult,
    SessionConfig,
    ToolStatusPulse,
    ToolStepStatus,
)

__all__ = [
    "AgentContext",
    "AgentHandler",
    "ApprovalRequest",
    "ApprovalResult",
    "AudioTransport",
    "BaseVoiceAgentProxy",
    "SessionConfig",
    "ToolStatusPulse",
    "ToolStepStatus",
    "VoiceAgent",
    "from_callable",
    "from_crewai",
    "from_http",
    "from_langchain",
    "from_websocket",
    "run_mock_demo",
]

logger = logging.getLogger("voiceagent")

# Per-connection query-param overrides a WebSocket client may set
# (ws://host:port/?language=hi-IN&tone=warm). Whitelisted: everything else
# in SessionConfig is operator policy, not client preference.
_CLIENT_OVERRIDABLE = {"language", "tone", "voice_id", "system_prompt"}

# LLM presets for split-stack provider strings.
_LLM_PRESETS: dict[str, dict[str, str]] = {
    "groq": {
        "llm_base_url": "https://api.groq.com/openai/v1",
        "llm_model": "llama-3.3-70b-versatile",
        "llm_api_key_env": "GROQ_API_KEY",
    },
    "openai": {
        "llm_base_url": "https://api.openai.com/v1",
        "llm_model": "gpt-4o-mini",
        "llm_api_key_env": "OPENAI_API_KEY",
    },
}


def _resolve_provider(name: str) -> tuple[type[BaseVoiceAgentProxy], dict[str, Any]]:
    """Provider string -> (engine class, config updates).

    Lazy imports keep startup fast and optional deps optional.
    """
    from . import providers

    if name == "openai-realtime":
        # The Realtime wire is 24 kHz PCM16 both directions — not negotiable.
        return providers.OpenAIRealtimeProxy, {
            "input_sample_rate": 24_000, "output_sample_rate": 24_000,
        }
    if name == "gemini-live":
        return providers.GeminiLiveProxy, {
            "input_sample_rate": 16_000, "output_sample_rate": 24_000,
        }
    if name == "mock":
        return providers.MockProxy, {}
    if "+" in name:
        opts: dict[str, Any] = {}
        for part in name.split("+"):
            if part in ("deepgram", "deepgram-flux"):
                opts["asr"] = part
            elif part in ("cartesia", "elevenlabs"):
                opts["tts"] = part
            elif part in _LLM_PRESETS:
                opts.update(_LLM_PRESETS[part])
            else:
                raise ValueError(
                    f"Unknown split-stack part {part!r} in provider {name!r}; "
                    f"expected deepgram|deepgram-flux, groq|openai, cartesia|elevenlabs"
                )
        return providers.SplitStackProxy, {"provider_options": opts}
    raise ValueError(
        f"Unknown provider {name!r}. Use 'openai-realtime', 'gemini-live', "
        f"'mock', or an 'asr+llm+tts' combo like 'deepgram+groq+cartesia'."
    )


class VoiceAgent:
    """The facade: one object, a few kwargs, ``.run()``."""

    def __init__(
        self,
        provider: str = "mock",
        agent: AgentHandler | None = None,
        *,
        language: str = "en-US",
        tone: str = "neutral",
        system_prompt: str = "You are a helpful voice assistant.",
        voice: str | None = None,
        guardrails: str = "standard",
        config: SessionConfig | None = None,
        **provider_options: Any,
    ) -> None:
        self.provider = provider
        self._agent = from_callable(agent) if agent is not None else None
        base = config or SessionConfig()
        self._base_config = base.model_copy(update={
            "language": language,
            "tone": tone,
            "system_prompt": system_prompt,
            "voice_id": voice,
            "guardrail_profile": guardrails,
            "provider_options": {**base.provider_options, **provider_options},
        })
        # Fail fast on bad provider strings — at construction, not first call.
        _resolve_provider(provider)

    # -- session assembly ------------------------------------------------------

    def _make_session(
        self, transport: AudioTransport, overrides: dict[str, str] | None = None
    ) -> tuple[BaseVoiceAgentProxy, OrchestratorBridge]:
        # Local import: keeps `python -m voiceagent.guardrails_and_eval`
        # free of the double-import RuntimeWarning.
        from .guardrails_and_eval import GuardrailPipeline, TelemetryRecorder

        updates: dict[str, Any] = {
            k: v for k, v in (overrides or {}).items() if k in _CLIENT_OVERRIDABLE
        }
        cls, provider_updates = _resolve_provider(self.provider)
        merged_opts = {
            **self._base_config.provider_options,
            **provider_updates.pop("provider_options", {}),
        }
        config = self._base_config.model_copy(update={
            **updates,
            **provider_updates,
            "provider_options": merged_opts,
            "session_id": SessionConfig().session_id,  # fresh id per session
        })

        proxy = cls(config, transport)
        proxy.telemetry = TelemetryRecorder(config.session_id)

        handler = self._agent
        if handler is None:
            handler = self._default_agent(proxy)

        pipeline = (
            None
            if config.guardrail_profile == "off"
            else GuardrailPipeline(config.guardrail_profile, config.language)
        )
        bridge = OrchestratorBridge(proxy, handler, config, pipeline, proxy.telemetry)
        proxy.bind(bridge)
        return proxy, bridge

    def _default_agent(self, proxy: BaseVoiceAgentProxy) -> AgentHandler:
        from .providers import MockProxy, SplitStackProxy, make_llm_agent

        if isinstance(proxy, SplitStackProxy):
            # No agent supplied -> plain streaming LLM chat over the stack's
            # own LLM router. Swap in your orchestrator whenever it's ready.
            return make_llm_agent(proxy.llm)
        if isinstance(proxy, MockProxy):
            async def echo(ctx: AgentContext) -> str:
                return f"You said: {ctx.text}"

            return echo
        raise ValueError(
            f"provider {self.provider!r} needs an agent= handler: native omni "
            f"engines proxy to YOUR backend (pass a function or an adapter "
            f"from voiceagent.adapters)."
        )

    # -- run modes ------------------------------------------------------------

    async def arun(self, transport: str | AudioTransport = "local") -> None:
        if not isinstance(transport, str):
            proxy, _ = self._make_session(transport)
            await proxy.run()
            return

        if transport == "local":
            cls_updates = _resolve_provider(self.provider)[1]
            local = LocalAudioTransport(
                input_sample_rate=cls_updates.get(
                    "input_sample_rate", self._base_config.input_sample_rate
                ),
                output_sample_rate=cls_updates.get(
                    "output_sample_rate", self._base_config.output_sample_rate
                ),
            )
            proxy, _ = self._make_session(local)
            await proxy.run()
            return

        if transport.startswith("ws://"):
            import websockets

            hostport = transport.removeprefix("ws://").rstrip("/")
            host, _, port_s = hostport.partition(":")
            port = int(port_s or 8765)
            input_rate = _resolve_provider(self.provider)[1].get(
                "input_sample_rate", self._base_config.input_sample_rate
            )

            async def handle(connection: Any) -> None:
                ws_transport = WebSocketTransport(connection, input_rate)
                proxy, _ = self._make_session(
                    ws_transport, ws_transport.config_overrides
                )
                try:
                    await proxy.run()
                except Exception:
                    logger.exception("session crashed")

            async with websockets.serve(handle, host or "0.0.0.0", port):
                logger.info("voiceagent listening on %s", transport)
                await asyncio.Future()  # serve until cancelled
            return

        raise ValueError(
            f"Unknown transport {transport!r}: use 'local', 'ws://host:port', "
            f"or pass an AudioTransport instance."
        )

    def run(self, transport: str | AudioTransport = "local") -> None:
        with contextlib.suppress(KeyboardInterrupt):
            asyncio.run(self.arun(transport))


async def run_mock_demo(
    agent: AgentHandler, utterances: list[str] | None = None
) -> list[str]:
    """Offline demo: drive ``agent`` through the mock engine, print the
    conversation, return everything that was 'spoken'."""
    from .guardrails_and_eval import MockTransport, _wait_until
    from .providers import MockProxy

    transport = MockTransport(packet_count=50)
    va = VoiceAgent(provider="mock", agent=agent, tone="neutral")
    proxy, _bridge = va._make_session(transport)
    assert isinstance(proxy, MockProxy)
    session = asyncio.create_task(proxy.run())

    script = utterances or [
        "hello there",
        "please delete the old files",
        "yes go ahead",
    ]
    for line in script:
        spoken_before = len(proxy.spoken)
        print(f"USER : {line}")
        proxy.push_user_utterance(line)
        # Wait for the pipeline to say something new, then let it settle:
        # keep waiting while speech is still arriving (commentary followed by
        # the final answer) so each turn prints complete.
        await _wait_until(lambda base=spoken_before: len(proxy.spoken) > base, 2.0)
        settled = len(proxy.spoken)
        while True:
            await asyncio.sleep(0.4)
            if len(proxy.spoken) == settled:
                break
            settled = len(proxy.spoken)
        for said in proxy.spoken[spoken_before:]:
            print(f"AGENT: {said}")

    proxy.end_script()
    await transport.close()
    await asyncio.wait_for(session, timeout=5.0)
    return proxy.spoken
