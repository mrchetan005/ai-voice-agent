"""Postgres-backed memory: durable across restarts, fail-open like Redis."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from typing import Any

from voiceagent.memory import Memory, Message

logger = logging.getLogger("voiceagent")

_DDL = """
CREATE TABLE IF NOT EXISTS memory_messages (
    key     text NOT NULL,
    role    text NOT NULL,
    content text NOT NULL,
    ts      double precision NOT NULL
);
CREATE INDEX IF NOT EXISTS memory_messages_key_ts ON memory_messages (key, ts);
"""


class PostgresMemory(Memory):
    def __init__(self, dsn: str | None, *, max_messages: int = 50) -> None:
        if not dsn:
            raise ValueError("postgres memory requires a database url")
        self._dsn = dsn
        self._max = max_messages
        self._pool: Any = None
        self._lock = asyncio.Lock()

    async def _get_pool(self) -> Any:
        async with self._lock:
            if self._pool is None:
                import asyncpg

                self._pool = await asyncpg.create_pool(self._dsn, min_size=1, max_size=4)
                async with self._pool.acquire() as conn:
                    await conn.execute(_DDL)
            return self._pool

    async def load(self, key: str, limit: int = 50) -> list[Message]:
        try:
            pool = await self._get_pool()
            rows = await pool.fetch(
                "SELECT role, content, ts FROM memory_messages WHERE key = $1 "
                "ORDER BY ts DESC LIMIT $2",
                key,
                limit,
            )
            return [
                Message(role=r["role"], content=r["content"], ts=r["ts"]) for r in reversed(rows)
            ]
        except Exception as exc:
            logger.warning("postgres memory load failed for %s: %s", key, exc)
            return []

    async def append(self, key: str, message: Message) -> None:
        try:
            pool = await self._get_pool()
            await pool.execute(
                "INSERT INTO memory_messages (key, role, content, ts) VALUES ($1, $2, $3, $4)",
                key,
                message["role"],
                message["content"],
                message["ts"],
            )
            await pool.execute(
                "DELETE FROM memory_messages WHERE key = $1 AND ts NOT IN "
                "(SELECT ts FROM memory_messages WHERE key = $1 ORDER BY ts DESC LIMIT $2)",
                key,
                self._max,
            )
        except Exception as exc:
            logger.warning("postgres memory append failed for %s: %s", key, exc)

    async def clear(self, key: str) -> None:
        try:
            pool = await self._get_pool()
            await pool.execute("DELETE FROM memory_messages WHERE key = $1", key)
        except Exception as exc:
            logger.warning("postgres memory clear failed for %s: %s", key, exc)

    async def aclose(self) -> None:
        if self._pool is not None:
            with contextlib.suppress(Exception):
                await self._pool.close()
