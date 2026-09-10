"""Conversation memory: durable context across turns and sessions.

The ABC is tiny on purpose: an ordered list of messages per key. Backends may
be process-local (dev), Redis (shared, TTL'd), or Postgres (durable). Memory
failures must never drop a live call — network-backed implementations are
fail-open: errors log and degrade to empty/no-op.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import TYPE_CHECKING, Literal, TypedDict

if TYPE_CHECKING:
    from voiceagent.config import MemoryConfig


class Message(TypedDict):
    role: Literal["user", "assistant"]
    content: str
    ts: float


class Memory(ABC):
    @abstractmethod
    async def load(self, key: str, limit: int = 50) -> list[Message]:
        """Most recent messages for ``key``, oldest first."""

    @abstractmethod
    async def append(self, key: str, message: Message) -> None: ...

    @abstractmethod
    async def clear(self, key: str) -> None: ...

    async def aclose(self) -> None:  # noqa: B027 (optional hook, default no-op)
        pass


def memory_from_config(
    cfg: MemoryConfig | None,
    *,
    redis_url: str | None = None,
    postgres_url: str | None = None,
) -> Memory | None:
    """Build a Memory backend; cfg.url wins, else the platform-level URLs."""
    if cfg is None:
        return None
    if cfg.backend == "inmemory":
        from voiceagent.memory.inmemory import InMemoryMemory

        return InMemoryMemory(max_messages=cfg.max_messages)
    if cfg.backend == "redis":
        from voiceagent.memory.redis import RedisMemory

        return RedisMemory(
            url=cfg.url or redis_url, max_messages=cfg.max_messages, ttl_s=cfg.ttl_s
        )
    from voiceagent.memory.postgres import PostgresMemory

    return PostgresMemory(dsn=cfg.url or postgres_url, max_messages=cfg.max_messages)


__all__ = ["Memory", "Message", "memory_from_config"]
