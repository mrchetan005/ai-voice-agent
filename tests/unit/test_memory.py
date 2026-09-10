"""Unit tests for conversation memory backends (offline, fakeredis for Redis)."""

from __future__ import annotations

from typing import TYPE_CHECKING

import fakeredis

from voiceagent.config import MemoryConfig
from voiceagent.memory import Message, memory_from_config
from voiceagent.memory.inmemory import InMemoryMemory
from voiceagent.memory.redis import RedisMemory

if TYPE_CHECKING:
    import pytest


def _msg(i: int, role: str = "user") -> Message:
    return {"role": role, "content": f"m{i}", "ts": float(i)}  # type: ignore[typeddict-item]


# --- InMemoryMemory ---


async def test_inmemory_load_returns_oldest_first() -> None:
    mem = InMemoryMemory()
    for i in range(3):
        await mem.append("k", _msg(i))

    assert await mem.load("k") == [_msg(0), _msg(1), _msg(2)]


async def test_inmemory_load_limit_keeps_most_recent() -> None:
    mem = InMemoryMemory()
    for i in range(5):
        await mem.append("k", _msg(i))

    assert await mem.load("k", limit=2) == [_msg(3), _msg(4)]


async def test_inmemory_max_messages_drops_oldest() -> None:
    mem = InMemoryMemory(max_messages=3)
    for i in range(5):
        await mem.append("k", _msg(i))

    assert await mem.load("k") == [_msg(2), _msg(3), _msg(4)]


async def test_inmemory_clear_empties_only_that_key() -> None:
    mem = InMemoryMemory()
    await mem.append("a", _msg(1))
    await mem.append("b", _msg(2))

    await mem.clear("a")

    assert await mem.load("a") == []
    assert await mem.load("b") == [_msg(2)]


async def test_inmemory_keys_are_isolated() -> None:
    mem = InMemoryMemory()
    await mem.append("a", _msg(1))
    await mem.append("b", _msg(2))

    assert await mem.load("a") == [_msg(1)]
    assert await mem.load("b") == [_msg(2)]


# --- memory_from_config ---


def test_memory_from_config_none_returns_none() -> None:
    assert memory_from_config(None) is None


async def test_memory_from_config_inmemory_honors_max_messages() -> None:
    mem = memory_from_config(MemoryConfig(backend="inmemory", max_messages=2))

    assert isinstance(mem, InMemoryMemory)
    for i in range(3):
        await mem.append("k", _msg(i))
    assert await mem.load("k") == [_msg(1), _msg(2)]


# --- RedisMemory (fakeredis) ---


def _fake_backed_memory(monkeypatch: pytest.MonkeyPatch, max_messages: int = 50) -> RedisMemory:
    fake = fakeredis.aioredis.FakeRedis(decode_responses=True)
    monkeypatch.setattr("redis.asyncio.from_url", lambda *args, **kwargs: fake)
    return RedisMemory(url=None, max_messages=max_messages)


async def test_redis_append_load_roundtrip(monkeypatch: pytest.MonkeyPatch) -> None:
    mem = _fake_backed_memory(monkeypatch)
    await mem.append("call1", _msg(1))
    await mem.append("call1", _msg(2, role="assistant"))

    assert await mem.load("call1") == [_msg(1), _msg(2, role="assistant")]
    await mem.aclose()


async def test_redis_caps_at_max_messages(monkeypatch: pytest.MonkeyPatch) -> None:
    mem = _fake_backed_memory(monkeypatch, max_messages=3)
    for i in range(5):
        await mem.append("k", _msg(i))

    assert await mem.load("k") == [_msg(2), _msg(3), _msg(4)]
    await mem.aclose()


async def test_redis_clear_removes_key(monkeypatch: pytest.MonkeyPatch) -> None:
    mem = _fake_backed_memory(monkeypatch)
    await mem.append("k", _msg(1))

    await mem.clear("k")

    assert await mem.load("k") == []
    await mem.aclose()


class _BrokenRedis:
    async def lrange(self, *args: object, **kwargs: object) -> list[str]:
        raise ConnectionError("redis down")

    def pipeline(self) -> _BrokenRedis:
        return self

    def rpush(self, *args: object) -> None:
        pass

    def ltrim(self, *args: object) -> None:
        pass

    def expire(self, *args: object) -> None:
        pass

    async def execute(self) -> None:
        raise ConnectionError("redis down")


async def test_redis_fails_open(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("redis.asyncio.from_url", lambda *args, **kwargs: _BrokenRedis())
    mem = RedisMemory(url="redis://nowhere:1/0")

    await mem.append("k", _msg(1))  # must not raise
    assert await mem.load("k") == []
