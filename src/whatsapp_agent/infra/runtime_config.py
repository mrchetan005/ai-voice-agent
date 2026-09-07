"""Runtime configuration: switch provider/brain/voice/model without env
edits or restarts.

Storage is one tiny Postgres table (voiceagent_config) with a 30 s Redis
cache in front; reads happen once per call/chat-session setup, never in
the audio hot path. Resolution precedence, highest first:

    request/CLI override  >  runtime config (DB)  >  Settings (env)

Only the keys in _KEYS are accepted — everything else in Settings
(tokens, URLs, credentials) deliberately stays env-only.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

from whatsapp_agent.config import get_settings
from whatsapp_agent.infra.redis import RedisGateway
from whatsapp_agent.infra.stores import _PgStore

logger = logging.getLogger("whatsapp_agent")


def _one_of(*allowed: str) -> Callable[[Any], str]:
    def validate(value: Any) -> str:
        if value not in allowed:
            raise ValueError(f"must be one of {allowed}")
        return value

    return validate


def _valid_provider(value: Any) -> str:
    from whatsapp_agent.channels.call_manager import PROVIDERS  # lazy: no cycle

    if value not in PROVIDERS:
        raise ValueError(f"must be one of {PROVIDERS}")
    return value


def _string(value: Any) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("must be a non-empty string")
    return value.strip()


def _boolean(value: Any) -> bool:
    if not isinstance(value, bool):
        raise ValueError("must be true or false")
    return value


def _int_min(minimum: int) -> Callable[[Any], int]:
    def validate(value: Any) -> int:
        if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
            raise ValueError(f"must be an integer >= {minimum}")
        return value

    return validate


# key -> (Settings attribute it falls back to, validator).
_KEYS: dict[str, tuple[str, Callable[[Any], Any]]] = {
    "provider": ("default_provider", _valid_provider),
    "brain": ("default_brain", _one_of("single", "dual")),
    "voice": ("voiceagent_voice_id", _string),
    "scheduler_model": ("scheduler_model", _string),
    "split_asr": ("split_asr", _string),
    "split_asr_model": ("split_asr_model", _string),
    "split_asr_language": ("split_asr_language", _string),
    "split_tts": ("split_tts", _string),
    "split_tts_model": ("split_tts_model", _string),
    "recap_enabled": ("recap_enabled", _boolean),
    "max_concurrent_calls": ("max_concurrent_calls", _int_min(0)),
}


class RuntimeConfig(_PgStore):
    """Same conventions as the other stores: connect() bootstraps the
    table, DB-down degrades to env-only resolution (never blocks a call)."""

    _TABLE_SQL = (
        "CREATE TABLE IF NOT EXISTS voiceagent_config ("
        "key text PRIMARY KEY, "
        "value jsonb NOT NULL, "
        "updated_at timestamptz NOT NULL DEFAULT now())",
    )
    _CACHE_KEY = "cfg:runtime"
    _CACHE_TTL_S = 30

    def __init__(self, db_url: str, redis: RedisGateway | None = None) -> None:
        super().__init__(db_url)
        self._redis = redis or RedisGateway(None)

    async def overrides(self) -> dict[str, Any]:
        """Current runtime overrides (allow-listed keys only)."""
        cached = await self._redis.get_json(self._CACHE_KEY)
        if cached is not None:
            return cached
        cursor = await self._execute("SELECT key, value FROM voiceagent_config", ())
        if cursor is None:  # degraded: env-only, and don't cache the gap
            return {}
        rows = {k: v for k, v in await cursor.fetchall() if k in _KEYS}
        await self._redis.set_json(self._CACHE_KEY, rows, ttl_s=self._CACHE_TTL_S)
        return rows

    async def set(self, key: str, value: Any) -> None:
        """Upsert one override (value=None clears it). Raises ValueError on
        unknown keys / invalid values — the API route surfaces it as 422."""
        if key not in _KEYS:
            raise ValueError(f"unknown config key {key!r} (allowed: {sorted(_KEYS)})")
        if value is None:
            await self._execute("DELETE FROM voiceagent_config WHERE key = %s", (key,))
        else:
            value = _KEYS[key][1](value)
            from psycopg.types.json import Jsonb

            await self._execute(
                "INSERT INTO voiceagent_config (key, value) VALUES (%s, %s) "
                "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, "
                "updated_at = now()",
                (key, Jsonb(value)),
            )
        await self._redis.delete(self._CACHE_KEY)

    async def resolve(self, key: str, override: Any = None) -> Any:
        """request/CLI override > runtime config > Settings."""
        if override is not None:
            return override
        overrides = await self.overrides()
        if key in overrides:
            return overrides[key]
        return getattr(get_settings(), _KEYS[key][0])

    async def snapshot(self) -> dict[str, dict[str, Any]]:
        """{key: {value, source}} for GET /config."""
        overrides = await self.overrides()
        settings = get_settings()
        return {
            key: {
                "value": overrides.get(key, getattr(settings, attr)),
                "source": "runtime" if key in overrides else "default",
            }
            for key, (attr, _) in _KEYS.items()
        }
