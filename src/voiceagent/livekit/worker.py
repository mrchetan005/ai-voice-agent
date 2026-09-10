"""Worker runtime: serve VoiceAgents on a livekit-agents AgentServer.

One worker registers a single LiveKit agent_name (settings.agent_source
.worker_name) and serves ALL loaded agents — the dispatch metadata's
`agent_id` selects which VoiceAgent runs a given session. Worker fleets are
therefore homogeneous and scale as one Deployment.
"""

from __future__ import annotations

import importlib
import logging
import time
from typing import Any

from livekit.agents import AgentServer, AutoSubscribe, JobContext, JobProcess, cli

from voiceagent.agent import VoiceAgent
from voiceagent.config import load_agents_yaml
from voiceagent.events import SessionEnded, SessionIDs, SessionStarted
from voiceagent.livekit.bridge import attach_bridge, make_dispatcher, spawn
from voiceagent.livekit.compile import SessionRuntime, build_session
from voiceagent.livekit.providers import missing_provider_keys
from voiceagent.livekit.recording import start_room_recording
from voiceagent.memory import memory_from_config
from voiceagent.metadata import SessionMetadata
from voiceagent.settings import Settings, SettingsError, load_settings
from voiceagent.store import SessionStore, StatusStore

logger = logging.getLogger("voiceagent")

BARGE_IN_TOPIC = "va.control"
BARGE_IN_CLEAR = b"clear"


def validate_agents(agents: list[VoiceAgent]) -> None:
    if not agents:
        raise SettingsError(
            "no agents to serve — set VOICEAGENT_AGENTS or VOICEAGENT_AGENTS_FILE, "
            "or pass agents to run()"
        )
    names = [a.name for a in agents]
    if len(set(names)) != len(names):
        raise SettingsError(f"duplicate agent names: {names}")
    missing: set[str] = set()
    for agent in agents:
        cfg = agent.config
        if cfg.mode == "realtime":
            assert cfg.realtime is not None
            missing.update(missing_provider_keys("realtime", cfg.realtime.provider))
        else:
            assert cfg.llm and cfg.stt and cfg.tts
            missing.update(missing_provider_keys("llm", cfg.llm.provider))
            missing.update(missing_provider_keys("stt", cfg.stt.provider))
            missing.update(missing_provider_keys("tts", cfg.tts.provider))
    if missing:
        raise SettingsError(
            "missing provider API keys for configured agents: " + ", ".join(sorted(missing))
        )


def _prewarm(proc: JobProcess) -> None:
    from livekit.plugins import silero

    proc.userdata["vad"] = silero.VAD.load()


async def _run_session(ctx: JobContext, agents_by_name: dict[str, VoiceAgent],
                       settings: Settings) -> None:
    meta = SessionMetadata.from_json(ctx.job.metadata)
    agent_id = meta.agent_id or (next(iter(agents_by_name)) if len(agents_by_name) == 1 else "")
    va = agents_by_name.get(agent_id)
    if va is None:
        logger.error(
            "no agent %r on this worker (serving: %s) — rejecting session",
            agent_id, sorted(agents_by_name),
        )
        ctx.shutdown(reason=f"unknown agent {agent_id!r}")
        return

    await ctx.connect(auto_subscribe=AutoSubscribe.AUDIO_ONLY)

    ids = SessionIDs(
        tenant_id=meta.tenant_id,
        agent_id=va.name,
        session_id=meta.session_id or ctx.job.id,
        room_id=ctx.room.name,
        call_id=meta.call_id,
        user_id=meta.user_id,
        channel=meta.channel,
    )
    status = StatusStore(settings.platform.redis_url)
    store = SessionStore(settings.platform.database_url)
    memory = memory_from_config(va.config.memory)
    memory_key = meta.memory_key or meta.user_id or ids.session_id

    runtime = SessionRuntime(ids=ids, memory=memory, memory_key=memory_key)
    runtime.emit = make_dispatcher(va.event_handlers)

    history = await memory.load(memory_key) if memory else []
    vad = ctx.proc.userdata.get("vad") if va.config.mode == "pipeline" else None
    agent, session = build_session(va, meta, runtime, vad=vad, history=history)
    attach_bridge(session, runtime)

    started_at = time.time()
    close_info: dict[str, str] = {"reason": "completed"}

    def on_close(ev: Any) -> None:
        close_info["reason"] = str(getattr(ev, "reason", "completed"))

    def on_agent_state(ev: Any) -> None:
        # Channel bridges (e.g. WhatsApp) flush their local playout buffer
        # whenever the agent stops speaking — this is the barge-in signal.
        if str(ev.old_state) == "speaking" and str(ev.new_state) != "speaking":
            spawn(
                ctx.room.local_participant.publish_data(BARGE_IN_CLEAR, topic=BARGE_IN_TOPIC)
            )

    session.on("close", on_close)
    session.on("agent_state_changed", on_agent_state)

    recording_path: str | None = None

    async def finalize(reason: str = "") -> None:
        usage, cost = runtime.usage.finalize()
        duration = time.time() - started_at
        runtime.emit(
            SessionEnded(ids=ids, reason=close_info["reason"], duration_s=duration)
        )
        await status.update(
            ids.session_id,
            status="ended",
            reason=close_info["reason"],
            duration_s=round(duration, 1),
            cost_usd=cost["total_usd"],
        )
        await store.finish(
            session_id=ids.session_id,
            status="ended",
            usage=usage,
            cost_usd=cost["total_usd"],
            recording_url=recording_path,
        )
        if memory:
            await memory.aclose()
        await status.aclose()
        await store.aclose()

    ctx.add_shutdown_callback(finalize)

    await session.start(agent, room=ctx.room)
    runtime.emit(SessionStarted(ids=ids))
    await status.update(
        ids.session_id, status="active", room=ids.room_id, agent_id=ids.agent_id
    )

    if settings.recording.enabled and meta.record:
        recording_path = await start_room_recording(
            ctx.api, room_name=ctx.room.name, rec=settings.recording, ids=ids
        )

    if va.config.greeting:
        session.generate_reply(instructions=va.config.greeting)


def build_server(agents: list[VoiceAgent], settings: Settings | None = None) -> AgentServer:
    settings = settings or load_settings()
    validate_agents(agents)
    agents_by_name = {a.name: a for a in agents}

    ws = settings.worker
    server = AgentServer(
        setup_fnc=_prewarm,
        drain_timeout=ws.drain_timeout_s,
        load_threshold=ws.load_threshold,
        num_idle_processes=ws.idle_processes,
        prometheus_port=ws.prometheus_port,
        ws_url=settings.livekit.url or None,
        api_key=settings.livekit.api_key or None,
        api_secret=settings.livekit.api_secret or None,
    )

    @server.rtc_session(agent_name=settings.agent_source.worker_name)
    async def entrypoint(ctx: JobContext) -> None:
        await _run_session(ctx, agents_by_name, settings)

    logger.info(
        "worker %r serving agents: %s",
        settings.agent_source.worker_name,
        sorted(agents_by_name),
    )
    return server


def run(*agents: VoiceAgent) -> None:
    """Entry for `voiceagent.run(agent)`: boots the livekit worker CLI."""
    settings = load_settings()
    cli.run_app(build_server(list(agents), settings))


def load_agents_from_settings(settings: Settings) -> list[VoiceAgent]:
    src = settings.agent_source
    agents: list[VoiceAgent] = []
    if src.agents:
        for path in filter(None, (p.strip() for p in src.agents.split(","))):
            module_path, _, attr = path.partition(":")
            if not attr:
                raise SettingsError(f"VOICEAGENT_AGENTS entry {path!r} must be 'pkg.module:attr'")
            obj = getattr(importlib.import_module(module_path), attr)
            if callable(obj) and not isinstance(obj, VoiceAgent):
                obj = obj()
            if isinstance(obj, VoiceAgent):
                agents.append(obj)
            elif isinstance(obj, list | tuple):
                agents.extend(obj)
            else:
                raise SettingsError(f"{path!r} resolved to {type(obj).__name__}, not VoiceAgent(s)")
    if src.agents_file:
        agents.extend(VoiceAgent.from_config(c) for c in load_agents_yaml(src.agents_file))
    return agents


def worker_main() -> None:
    """Console script `voiceagent-worker`: agents from env, then the livekit CLI."""
    logging.basicConfig(level=logging.INFO)
    settings = load_settings()
    agents = load_agents_from_settings(settings)
    cli.run_app(build_server(agents, settings))
