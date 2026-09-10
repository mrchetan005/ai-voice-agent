"""Process-local memory for development and tests."""

from __future__ import annotations

from collections import defaultdict, deque

from voiceagent.memory import Memory, Message


class InMemoryMemory(Memory):
    def __init__(self, max_messages: int = 50) -> None:
        self._store: defaultdict[str, deque[Message]] = defaultdict(
            lambda: deque(maxlen=max_messages)
        )

    async def load(self, key: str, limit: int = 50) -> list[Message]:
        return list(self._store[key])[-limit:]

    async def append(self, key: str, message: Message) -> None:
        self._store[key].append(message)

    async def clear(self, key: str) -> None:
        self._store.pop(key, None)
