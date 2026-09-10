"""Agent catalog: what this deployment serves."""

from __future__ import annotations

from fastapi import APIRouter, Request

router = APIRouter()


@router.get("/v1/agents")
async def list_agents(request: Request) -> list[dict]:
    out = []
    for va in request.app.state.agents.values():
        cfg = va.config
        providers = (
            {"realtime": cfg.realtime.provider}
            if cfg.mode == "realtime" and cfg.realtime
            else {
                "llm": cfg.llm.provider if cfg.llm else None,
                "stt": cfg.stt.provider if cfg.stt else None,
                "tts": cfg.tts.provider if cfg.tts else None,
            }
        )
        out.append(
            {
                "agent_id": va.name,
                "mode": cfg.mode,
                "language": cfg.language,
                "providers": providers,
                "tools": [t.name for t in va.tools],
            }
        )
    return out
