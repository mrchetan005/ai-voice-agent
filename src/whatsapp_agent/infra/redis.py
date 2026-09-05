"""Redis gateway: dedup, rate limiting, cache-aside and locks — ALL fail-open.

An outage must never break a live call or drop a webhook: with REDIS_URL
unset (disabled) or Redis unreachable, every operation degrades to the
permissive answer — "not a duplicate", "not rate limited", "cache miss",
"lock granted". Socket timeouts are 0.5 s, so a dead Redis costs at most
half a second per operation, never a hang.

Key schema (TTLs owned by the callers):
    wa:dedup:{event_id}                  600 s   webhook retry dedup
    wa:rl:phone:{phone}                  60 s    inbound per-phone rate limit
    cal:slots:{et}:{start}:{end}:{tz}    90 s    Cal.com slots cache
    profile:{phone}                      300 s   caller profile cache
    call:active:{phone}                  7200 s  one call per phone (lock)
"""

from __future__ import annotations

import contextlib
import json
import logging
import time
import uuid
from typing import Any

logger = logging.getLogger("whatsapp_agent")


class RedisGateway:
    """Thin wrapper over redis.asyncio with the fail-open policy baked in.
    Construct with url=None for a disabled gateway — callers never need
    None checks, every method just returns its permissive default."""

    def __init__(self, url: str | None = None) -> None:
        self._redis: Any = None
        if url:
            import redis.asyncio as aioredis

            self._redis = aioredis.from_url(
                url, decode_responses=True,
                socket_timeout=0.5, socket_connect_timeout=0.5,
            )

    @property
    def enabled(self) -> bool:
        return self._redis is not None

    async def ping(self) -> bool:
        if self._redis is None:
            return False
        try:
            return bool(await self._redis.ping())
        except Exception:
            return False

    async def dedup_seen(self, key: str, ttl_s: int = 600) -> bool:
        """True if this key was already recorded (i.e. a duplicate).
        First sighting records it atomically (SET NX EX)."""
        if self._redis is None:
            return False
        try:
            return not await self._redis.set(key, "1", nx=True, ex=ttl_s)
        except Exception as exc:
            logger.warning("redis dedup failed (allowing): %s", exc)
            return False

    async def rate_limited(self, key: str, limit: int, window_s: int = 60) -> bool:
        """Sliding-window counter (zset of timestamps). True = over limit."""
        if self._redis is None:
            return False
        try:
            now = time.time()
            pipe = self._redis.pipeline()
            pipe.zremrangebyscore(key, 0, now - window_s)
            pipe.zadd(key, {f"{now:.6f}:{uuid.uuid4().hex[:8]}": now})
            pipe.zcard(key)
            pipe.expire(key, window_s)
            results = await pipe.execute()
            return int(results[2]) > limit
        except Exception as exc:
            logger.warning("redis rate limit failed (allowing): %s", exc)
            return False

    async def get_json(self, key: str) -> Any:
        if self._redis is None:
            return None
        try:
            raw = await self._redis.get(key)
            return json.loads(raw) if raw else None
        except Exception as exc:
            logger.warning("redis get failed (cache miss): %s", exc)
            return None

    async def set_json(self, key: str, value: Any, ttl_s: int) -> None:
        if self._redis is None:
            return
        try:
            await self._redis.set(key, json.dumps(value), ex=ttl_s)
        except Exception as exc:
            logger.warning("redis set failed (skipped): %s", exc)

    async def delete(self, key: str) -> None:
        if self._redis is None:
            return
        try:
            await self._redis.delete(key)
        except Exception as exc:
            logger.warning("redis delete failed: %s", exc)

    async def acquire_lock(self, key: str, token: str, ttl_s: int) -> bool:
        """Fail-open TRUE: Redis down must never stop a call — the
        in-process CallManager lock still serializes this worker."""
        if self._redis is None:
            return True
        try:
            return bool(await self._redis.set(key, token, nx=True, ex=ttl_s))
        except Exception as exc:
            logger.warning("redis lock failed (granting): %s", exc)
            return True

    async def release_lock(self, key: str, token: str) -> None:
        """Release only if we still hold it. GET-then-DEL has a tiny race
        window; acceptable — one owner per phone, TTL is the backstop."""
        if self._redis is None:
            return
        try:
            if await self._redis.get(key) == token:
                await self._redis.delete(key)
        except Exception as exc:
            logger.warning("redis unlock failed (TTL will expire it): %s", exc)

    async def aclose(self) -> None:
        if self._redis is not None:
            with contextlib.suppress(Exception):
                await self._redis.aclose()
