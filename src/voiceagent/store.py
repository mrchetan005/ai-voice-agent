"""Session persistence: Redis for live status, Postgres for durable records.

Shared by the worker (writes) and the platform API (reads + initial insert).
Both stores are fail-open: persistence must never take down a live session,
so every error logs and degrades to a no-op / None.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import json
import logging
from typing import Any

logger = logging.getLogger("voiceagent")

_STATUS_PREFIX = "va:session:"
_STATUS_TTL_S = 86_400


class StatusStore:
    """Ephemeral session status in Redis (24h TTL)."""

    def __init__(self, url: str | None) -> None:
        self._redis = None
        if url:
            try:
                import redis.asyncio as aioredis

                self._redis = aioredis.from_url(
                    url, socket_timeout=0.5, socket_connect_timeout=0.5, decode_responses=True
                )
            except Exception as exc:
                logger.warning("status store disabled (redis init failed: %s)", exc)

    async def update(self, session_id: str, **fields: Any) -> None:
        if self._redis is None or not session_id:
            return
        try:
            key = _STATUS_PREFIX + session_id
            raw = await self._redis.get(key)
            payload = json.loads(raw) if raw else {}
            payload.update(fields)
            await self._redis.set(key, json.dumps(payload, default=str), ex=_STATUS_TTL_S)
        except Exception as exc:
            logger.warning("status update failed for %s: %s", session_id, exc)

    async def get(self, session_id: str) -> dict[str, Any] | None:
        if self._redis is None:
            return None
        try:
            raw = await self._redis.get(_STATUS_PREFIX + session_id)
            return json.loads(raw) if raw else None
        except Exception as exc:
            logger.warning("status read failed for %s: %s", session_id, exc)
            return None

    async def ping(self) -> str:
        if self._redis is None:
            return "disabled"
        try:
            await self._redis.ping()
            return "ok"
        except Exception as exc:
            return f"error: {exc.__class__.__name__}"

    async def aclose(self) -> None:
        if self._redis is not None:
            import contextlib

            with contextlib.suppress(Exception):
                await self._redis.aclose()


class SessionStore:
    """Durable session records in Postgres (see deploy/compose/init.sql)."""

    def __init__(self, dsn: str | None) -> None:
        self._dsn = dsn or None
        self._pool: Any = None
        self._lock = asyncio.Lock()

    async def _get_pool(self) -> Any:
        async with self._lock:
            if self._pool is None and self._dsn:
                import asyncpg

                self._pool = await asyncpg.create_pool(self._dsn, min_size=1, max_size=4)
            return self._pool

    async def insert(
        self,
        *,
        session_id: str,
        tenant_id: str,
        agent_id: str,
        channel: str,
        room: str,
        user_id: str | None,
    ) -> None:
        try:
            pool = await self._get_pool()
            if pool is None:
                return
            await pool.execute(
                "INSERT INTO sessions (id, tenant_id, agent_id, channel, room, user_id, status) "
                "VALUES ($1, $2, $3, $4, $5, $6, 'pending') ON CONFLICT (id) DO NOTHING",
                session_id,
                tenant_id,
                agent_id,
                channel,
                room,
                user_id,
            )
        except Exception as exc:
            logger.warning("session insert failed for %s: %s", session_id, exc)

    async def finish(
        self,
        *,
        session_id: str,
        status: str,
        usage: dict[str, Any] | None = None,
        cost_usd: float | None = None,
        recording_url: str | None = None,
    ) -> None:
        try:
            pool = await self._get_pool()
            if pool is None:
                return
            await pool.execute(
                "UPDATE sessions SET status = $2, ended_at = $3, usage = $4, "
                "cost_usd = $5, recording_url = COALESCE($6, recording_url) WHERE id = $1",
                session_id,
                status,
                dt.datetime.now(dt.UTC),
                json.dumps(usage) if usage is not None else None,
                cost_usd,
                recording_url,
            )
        except Exception as exc:
            logger.warning("session finish failed for %s: %s", session_id, exc)

    async def get(self, session_id: str) -> dict[str, Any] | None:
        try:
            pool = await self._get_pool()
            if pool is None:
                return None
            row = await pool.fetchrow("SELECT * FROM sessions WHERE id = $1", session_id)
            return dict(row) if row else None
        except Exception as exc:
            logger.warning("session read failed for %s: %s", session_id, exc)
            return None

    async def aclose(self) -> None:
        if self._pool is not None:
            import contextlib

            with contextlib.suppress(Exception):
                await self._pool.close()
