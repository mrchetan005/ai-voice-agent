# Memory

Conversation memory is durable context across turns and sessions: an ordered list of messages per key, behind a tiny ABC with three backends.

Network-backed backends are **fail-open** — a memory error logs and degrades to empty/no-op so it can never take down a live call.

## The ABC

```python
# voiceagent/memory/__init__.py
class Message(TypedDict):
    role: Literal["user", "assistant"]
    content: str
    ts: float                      # unix seconds

class Memory(ABC):
    async def load(self, key: str, limit: int = 50) -> list[Message]: ...   # oldest first
    async def append(self, key: str, message: Message) -> None: ...
    async def clear(self, key: str) -> None: ...
    async def aclose(self) -> None: ...                                      # default no-op
```

- `load` returns the most recent `limit` messages, **oldest first** (ready to replay into a chat context).
- `Message` roles are only `user` / `assistant`.
- `aclose` releases connections; override it in a backend that holds any.

At runtime the worker derives the key as `memory_key` → `user_id` → `session_id` (first non-empty), so a returning caller resumes their own history.

## Backends

| backend | class | shared? | durable? | TTL | needs |
|---------|-------|---------|----------|-----|-------|
| `inmemory` | `InMemoryMemory` | no (process-local) | no | none | nothing |
| `redis` | `RedisMemory` | yes (across workers) | until TTL | `ttl_s` (default 7d) | `redis` extra + `url` |
| `postgres` | `PostgresMemory` | yes | yes (across restarts) | none (pruned by count) | `asyncpg` + `dsn` |

All three cap stored history at `max_messages` (default 50) per key.

### inmemory

Process-local, for dev and tests. A bounded `deque(maxlen=max_messages)` per key; `clear` drops the key. Nothing survives a restart and nothing is shared between workers.

```python
from voiceagent.memory.inmemory import InMemoryMemory
mem = InMemoryMemory(max_messages=50)
```

### redis

Shared across workers, TTL'd, fail-open. One Redis list per key under the prefix `va:memory:`; `append` runs `rpush` + `ltrim` (to `max_messages`) + `expire` (`ttl_s`) in a pipeline. Socket connect/read timeouts are 0.5s. Any Redis error logs a warning and degrades — `load` returns `[]`, `append`/`clear` become no-ops.

```python
from voiceagent.memory.redis import RedisMemory
mem = RedisMemory("redis://localhost:6379/0", max_messages=50, ttl_s=604_800)
```

`url` defaults to `redis://localhost:6379/0` when `None`.

### postgres

Durable across restarts, fail-open like Redis, backed by `asyncpg`. On first use it creates a connection pool (`min_size=1, max_size=4`) and ensures the table `memory_messages(key, role, content, ts)` with an index on `(key, ts)`. `append` inserts then prunes rows beyond `max_messages` for that key; `load` reads newest-first with a `LIMIT` and reverses to oldest-first. There is **no TTL** — history is bounded only by `max_messages`.

```python
from voiceagent.memory.postgres import PostgresMemory
mem = PostgresMemory("postgresql://user:pass@host/db", max_messages=50)
```

`dsn` is required — constructing with `None` raises `ValueError`.

## Building from config

`MemoryConfig` selects the backend declaratively (see [configuration](configuration.md)):

```python
class MemoryConfig(BaseModel):
    backend: Literal["inmemory", "redis", "postgres"] = "inmemory"
    url: str | None = None          # falls back to platform settings when None
    max_messages: int = 50
    ttl_s: int = 604_800            # redis only
```

`memory_from_config` builds the backend. `cfg.url` wins; otherwise the platform-level URL is used (`redis_url` for redis, `postgres_url` for postgres). `cfg=None` returns `None` (no memory):

```python
from voiceagent import memory_from_config

memory = memory_from_config(
    cfg,                        # MemoryConfig | None
    redis_url=settings.platform.redis_url or None,
    postgres_url=settings.platform.database_url or None,
)
```

In the sugar API you can pass a bare string, which becomes `MemoryConfig(backend=...)`:

```python
agent = VoiceAgent(name="demo", llm="google", memory="redis", system_prompt="…")
```

Connection URLs are env-only — supply them via platform settings (`PLATFORM_REDIS_URL`, `PLATFORM_DATABASE_URL`) or `MemoryConfig.url`, never hard-coded.

## See also

- [configuration](configuration.md) — the `memory` block and platform URLs.
- [concepts](concepts.md) — how memory fits the session lifecycle.
- [tools](tools.md) — tools receive the live `Memory` via `ToolContext`.
