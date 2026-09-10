"""Worker runtime: serve VoiceAgents on a livekit-agents AgentServer.

One worker registers a single LiveKit agent_name (settings.agent_source
.worker_name) and serves ALL loaded agents — the dispatch metadata's
`agent_id` selects which VoiceAgent runs a given session. Worker fleets are
therefore homogeneous and scale as one Deployment.
"""

from __future__ import annotations

import inspect
import logging
import os
import time
from typing import Any

from livekit.agents import AgentServer, AutoSubscribe, JobContext, JobProcess, cli

from voiceagent.agent import VoiceAgent, load_agents
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


def missing_keys_by_agent(agents: list[VoiceAgent]) -> dict[str, list[str]]:
    """Per-agent missing provider env vars (empty dict = everything servable)."""
    out: dict[str, list[str]] = {}
    for agent in agents:
        cfg = agent.config
        missing: set[str] = set()
        if cfg.mode == "realtime":
            assert cfg.realtime is not None
            missing.update(missing_provider_keys("realtime", cfg.realtime.provider))
        else:
            assert cfg.llm and cfg.stt and cfg.tts
            missing.update(missing_provider_keys("llm", cfg.llm.provider))
            missing.update(missing_provider_keys("stt", cfg.stt.provider))
            missing.update(missing_provider_keys("tts", cfg.tts.provider))
        if missing:
            out[agent.name] = sorted(missing)
    return out


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
    memory = memory_from_config(
        va.config.memory,
        redis_url=settings.platform.redis_url or None,
        postgres_url=settings.platform.database_url or None,
    )
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
    unservable = missing_keys_by_agent(agents)
    for name, missing in unservable.items():
        logger.error(
            "agent %r DISABLED on this worker — missing provider keys: %s",
            name, ", ".join(missing),
        )
    ready = [a for a in agents if a.name not in unservable]
    if not ready:
        raise SettingsError(
            "no servable agents — missing provider API keys: "
            + "; ".join(f"{n}: {', '.join(m)}" for n, m in unservable.items())
        )
    agents_by_name = {a.name: a for a in ready}

    # Job subprocesses (Linux) pickle the entrypoint BY REFERENCE, so it must
    # be a module-level function; state lives in a module global that child
    # processes rebuild from the environment (see _worker_state).
    global _WORKER_STATE
    _WORKER_STATE = (agents_by_name, settings)

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
    server.rtc_session(agent_name=settings.agent_source.worker_name)(_entrypoint)

    logger.info(
        "worker %r serving agents: %s",
        settings.agent_source.worker_name,
        sorted(agents_by_name),
    )
    return server


_WORKER_STATE: tuple[dict[str, VoiceAgent], Settings] | None = None


def _worker_state() -> tuple[dict[str, VoiceAgent], Settings]:
    """Worker state; job subprocesses rebuild it from the environment."""
    global _WORKER_STATE
    if _WORKER_STATE is None:
        settings = load_settings()
        agents = load_agents(settings)
        unservable = missing_keys_by_agent(agents)
        _WORKER_STATE = ({a.name: a for a in agents if a.name not in unservable}, settings)
    return _WORKER_STATE


async def _entrypoint(ctx: JobContext) -> None:
    agents_by_name, settings = _worker_state()
    await _run_session(ctx, agents_by_name, settings)


def _export_agents_env(agents: tuple[VoiceAgent, ...]) -> None:
    """Make programmatic agents recoverable in spawned job subprocesses.

    Finds each agent as a module-level attribute of the calling script and
    exports VOICEAGENT_AGENTS so a child process can re-import them ("__main__"
    resolves to the re-imported entry script under multiprocessing spawn).
    """
    if os.environ.get("VOICEAGENT_AGENTS") or os.environ.get("VOICEAGENT_AGENTS_FILE"):
        return
    caller = next(
        (
            f.frame
            for f in inspect.stack()
            if not f.frame.f_globals.get("__name__", "").startswith("voiceagent")
        ),
        None,
    )
    if caller is None:
        return
    module_name = caller.f_globals.get("__name__", "")
    specs: list[str] = []
    for agent in agents:
        attr = next(
            (name for name, val in caller.f_globals.items() if val is agent), None
        )
        if attr is None:
            logger.warning(
                "agent %r is not a module-level variable of %s; job subprocesses "
                "will not find it — assign it at module level or use "
                "VOICEAGENT_AGENTS/VOICEAGENT_AGENTS_FILE",
                agent.name,
                module_name,
            )
            return
        specs.append(f"{module_name}:{attr}")
    os.environ["VOICEAGENT_AGENTS"] = ",".join(specs)


def run(*agents: VoiceAgent) -> None:
    """Entry for `voiceagent.run(agent)`: boots the livekit worker CLI."""
    _export_agents_env(agents)
    settings = load_settings()
    cli.run_app(build_server(list(agents), settings))


def worker_main() -> None:
    """Console script `voiceagent-worker`: agents from env, then the livekit CLI."""
    logging.basicConfig(level=logging.INFO)
    settings = load_settings()
    agents = load_agents(settings)
    cli.run_app(build_server(agents, settings))
