"""Redis-backed memory: shared across workers, TTL'd, fail-open.

Any Redis error logs and degrades (empty load, dropped append) — memory must
never take down a live voice session.
"""

from __future__ import annotations

import contextlib
import json
import logging

from voiceagent.memory import Memory, Message

logger = logging.getLogger("voiceagent")

_KEY_PREFIX = "va:memory:"


class RedisMemory(Memory):
    def __init__(self, url: str | None, *, max_messages: int = 50, ttl_s: int = 604_800) -> None:
        import redis.asyncio as aioredis

        self._redis = aioredis.from_url(
            url or "redis://localhost:6379/0",
            socket_timeout=0.5,
            socket_connect_timeout=0.5,
            decode_responses=True,
        )
        self._max = max_messages
        self._ttl = ttl_s

    async def load(self, key: str, limit: int = 50) -> list[Message]:
        try:
            raw = await self._redis.lrange(_KEY_PREFIX + key, -limit, -1)
            return [json.loads(item) for item in raw]
        except Exception as exc:
            logger.warning("redis memory load failed for %s: %s", key, exc)
            return []

    async def append(self, key: str, message: Message) -> None:
        try:
            rkey = _KEY_PREFIX + key
            pipe = self._redis.pipeline()
            pipe.rpush(rkey, json.dumps(message, separators=(",", ":")))
            pipe.ltrim(rkey, -self._max, -1)
            pipe.expire(rkey, self._ttl)
            await pipe.execute()
        except Exception as exc:
            logger.warning("redis memory append failed for %s: %s", key, exc)

    async def clear(self, key: str) -> None:
        try:
            await self._redis.delete(_KEY_PREFIX + key)
        except Exception as exc:
            logger.warning("redis memory clear failed for %s: %s", key, exc)

    async def aclose(self) -> None:
        with contextlib.suppress(Exception):
            await self._redis.aclose()
