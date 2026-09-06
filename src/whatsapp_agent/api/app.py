"""FastAPI application factory: ONE service for webhooks, inbound calls,
chat, admin observability and (via CallManager) API-triggered calls.

Deployment contract: exactly ONE uvicorn worker. The WebRTC media legs,
event queues and per-sender agents live in this process; a second worker
would split them. Scale up later = Redis pub/sub between workers, not more
workers of this app.

Run:
    uvicorn whatsapp_agent.api.app:create_app --factory --workers 1
    (or `whatsapp-agent serve` in dev)
"""

from __future__ import annotations

import asyncio
import contextlib
import logging

from fastapi import FastAPI

from whatsapp_agent.channels.events import EventRouter
from whatsapp_agent.config import Settings, get_settings, logging_setup

logger = logging.getLogger("whatsapp_agent")


def create_app(
    settings: Settings | None = None,
    *,
    start_call_manager: bool = True,
    start_chat_manager: bool = True,
) -> FastAPI:
    settings = settings or get_settings()
    logging_setup(settings)

    @contextlib.asynccontextmanager
    async def lifespan(app: FastAPI):
        # Heavy lifting is imported here so the module stays importable
        # (and testable) without any environment.
        from whatsapp_agent.api.routes.webhooks import make_webhook_gate
        from whatsapp_agent.capabilities.booking.cal_client import CalClient
        from whatsapp_agent.channels.call_manager import CallManager
        from whatsapp_agent.channels.chat_manager import ChatManager
        from whatsapp_agent.channels.client import WhatsAppClient
        from whatsapp_agent.infra.redis import RedisGateway
        from whatsapp_agent.infra.runtime_config import RuntimeConfig
        from whatsapp_agent.infra.stores import SessionStore

        if not settings.whatsapp_app_secret:
            logger.warning(
                "WHATSAPP_APP_SECRET unset — webhook signature validation is "
                "OFF (dev only; set it in production)"
            )
        redis = RedisGateway(settings.redis_url)
        app.state.redis = redis
        if redis.enabled:
            app.state.webhook_gate = make_webhook_gate(
                redis, settings.webhook_rate_per_min
            )
        else:
            logger.warning("REDIS_URL unset — dedup/rate-limit/caches disabled")
        wa = WhatsAppClient()
        cal = CalClient()
        session_store = SessionStore(settings.database_url)
        await session_store.connect()
        config = RuntimeConfig(settings.database_url, redis=redis)
        await config.connect()
        app.state.config = config
        calls = CallManager(app.state.router, wa, cal, session_store,
                            redis=redis, config=config)
        chat = ChatManager(app.state.router, wa, cal, session_store,
                           redis=redis, config=config)
        app.state.wa = wa
        app.state.cal = cal
        app.state.session_store = session_store
        app.state.calls = calls
        app.state.chat = chat
        if start_call_manager:
            await calls.start()
        if start_chat_manager:
            await chat.start()
        logger.info("whatsapp-agent up (calls=%s chat=%s)",
                    start_call_manager, start_chat_manager)
        try:
            yield
        finally:
            # Stop intake first, then give teardown a bounded grace — all
            # queue consumers are tracked tasks, so shutdown cannot hang.
            with contextlib.suppress(Exception, asyncio.TimeoutError):
                async with asyncio.timeout(settings.shutdown_grace_s):
                    await chat.stop()
                    await calls.stop()
            await session_store.close()
            await config.close()
            cal.close()
            await wa.aclose()
            await redis.aclose()
            logger.info("whatsapp-agent shut down cleanly")

    # Docs are disabled: this app shares its ingress with the public Meta
    # webhook URL; the admin surface is token-cloaked, the rest is minimal.
    app = FastAPI(
        title="whatsapp-agent", lifespan=lifespan,
        docs_url=None, redoc_url=None, openapi_url=None,
    )
    # The event router is pure in-memory plumbing — created eagerly so the
    # webhook route works in tests without running the lifespan.
    app.state.router = EventRouter()

    from whatsapp_agent.api.routes import admin, calls, config, health, webhooks

    app.include_router(webhooks.router)
    app.include_router(health.router)
    app.include_router(admin.router)
    app.include_router(config.router)
    app.include_router(calls.router)
    return app
