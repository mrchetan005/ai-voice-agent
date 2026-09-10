"""Platform control plane: FastAPI app factory and `voiceagent-api` entry."""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from voiceagent.agent import load_agents
from voiceagent.platform.auth import require_token
from voiceagent.platform.routes import agents as agents_routes
from voiceagent.platform.routes import health as health_routes
from voiceagent.platform.routes import sessions as sessions_routes
from voiceagent.platform.routes import sip as sip_routes
from voiceagent.settings import Settings, load_settings
from voiceagent.store import SessionStore, StatusStore

logger = logging.getLogger("voiceagent")


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or load_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.settings = settings
        app.state.agents = {a.name: a for a in load_agents(settings)}
        app.state.status = StatusStore(settings.platform.redis_url)
        app.state.store = SessionStore(settings.platform.database_url)
        logger.info("platform serving agents: %s", sorted(app.state.agents))
        yield
        await app.state.status.aclose()
        await app.state.store.aclose()

    app = FastAPI(title="voiceagent platform", docs_url=None, redoc_url=None, lifespan=lifespan)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],  # demo-friendly; restrict via an ingress in production
        allow_methods=["*"],
        allow_headers=["*"],
    )

    app.include_router(health_routes.router)
    auth = [Depends(require_token)]
    app.include_router(sessions_routes.router, dependencies=auth)
    app.include_router(agents_routes.router, dependencies=auth)
    app.include_router(sip_routes.router, dependencies=auth)

    demo_dir = Path(settings.platform.demo_dir)
    if demo_dir.is_dir():
        app.mount("/demo", StaticFiles(directory=str(demo_dir), html=True), name="demo")

    return app


def main() -> None:
    """Console script `voiceagent-api`."""
    import uvicorn

    logging.basicConfig(level=logging.INFO)
    settings = load_settings()
    uvicorn.run(
        create_app(settings),
        host="0.0.0.0",
        port=settings.platform.port,
        log_level="info",
    )
