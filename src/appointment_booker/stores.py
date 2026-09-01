"""Caller profiles + local booking records (Neon Postgres).

Same conventions as TranscriptStore: async psycopg autocommit, schema
bootstrapped on connect, every operation degrades to a warning no-op when
the DB is down — persistence must never block or break a live call.
"""

from __future__ import annotations

import datetime as dt
import logging
from typing import Any

logger = logging.getLogger("appointment_booker")


class _PgStore:
    """Shared connect/reconnect plumbing for the small stores below."""

    _TABLE_SQL: tuple[str, ...] = ()

    def __init__(self, db_url: str) -> None:
        self._db_url = db_url
        self._db: Any = None

    async def connect(self) -> None:
        import psycopg

        try:
            self._db = await psycopg.AsyncConnection.connect(
                self._db_url, autocommit=True, connect_timeout=10
            )
            for sql in self._TABLE_SQL:
                await self._db.execute(sql)
        except Exception as exc:
            logger.warning("%s unavailable (memory-only): %s", type(self).__name__, exc)
            self._db = None

    @property
    def degraded(self) -> bool:
        """True when running memory-only (DB unreachable)."""
        return self._db is None

    async def _execute(self, sql: str, params: tuple[Any, ...]) -> Any:
        """One reconnect retry — Neon suspends idle connections."""
        if self._db is None:
            return None
        for attempt in (1, 2):
            try:
                return await self._db.execute(sql, params)
            except Exception as exc:
                if attempt == 2:
                    logger.warning("%s query failed (dropped): %s", type(self).__name__, exc)
                    return None
                logger.info("%s conn stale, reconnecting: %s", type(self).__name__, exc)
                await self.connect()
                if self._db is None:
                    return None

    async def close(self) -> None:
        if self._db is not None:
            await self._db.close()


class ProfileStore(_PgStore):
    """phone -> name/email/timezone. Lets repeat callers confirm instead of
    re-dictating their details on every call."""

    _TABLE_SQL = (
        "CREATE TABLE IF NOT EXISTS voiceagent_profiles ("
        "phone text PRIMARY KEY, "
        "name text NOT NULL DEFAULT '', "
        "email text NOT NULL DEFAULT '', "
        "timezone text NOT NULL DEFAULT '', "
        "updated_at timestamptz NOT NULL DEFAULT now())",
    )

    async def load(self, phone: str) -> dict[str, str] | None:
        cursor = await self._execute(
            "SELECT name, email, timezone FROM voiceagent_profiles WHERE phone = %s",
            (phone,),
        )
        if cursor is None:
            return None
        row = await cursor.fetchone()
        if row is None:
            return None
        return {"name": row[0], "email": row[1], "timezone": row[2]}

    async def upsert(
        self,
        phone: str,
        name: str | None = None,
        email: str | None = None,
        timezone: str | None = None,
    ) -> None:
        """Partial update: None/empty fields never blank stored values."""
        await self._execute(
            "INSERT INTO voiceagent_profiles (phone, name, email, timezone) "
            "VALUES (%s, %s, %s, %s) "
            "ON CONFLICT (phone) DO UPDATE SET "
            "name = COALESCE(NULLIF(EXCLUDED.name, ''), voiceagent_profiles.name), "
            "email = COALESCE(NULLIF(EXCLUDED.email, ''), voiceagent_profiles.email), "
            "timezone = COALESCE(NULLIF(EXCLUDED.timezone, ''), voiceagent_profiles.timezone), "
            "updated_at = now()",
            (phone, name or "", email or "", timezone or ""),
        )


class BookingStore(_PgStore):
    """Local booking records — what makes duplicate guards, reschedule
    lookups and post-call recaps possible (Cal.com alone can't be queried
    by caller phone cheaply mid-call)."""

    _TABLE_SQL = (
        "CREATE TABLE IF NOT EXISTS voiceagent_bookings ("
        "id bigserial PRIMARY KEY, "
        "phone text NOT NULL, "
        "cal_booking_uid text NOT NULL UNIQUE, "
        "start_utc timestamptz NOT NULL, "
        "topic text NOT NULL DEFAULT '', "
        "status text NOT NULL DEFAULT 'confirmed', "
        "created_at timestamptz NOT NULL DEFAULT now())",
        "CREATE INDEX IF NOT EXISTS idx_voiceagent_bookings_phone "
        "ON voiceagent_bookings (phone, start_utc)",
    )

    async def add(self, phone: str, uid: str, start_utc: dt.datetime, topic: str) -> None:
        await self._execute(
            "INSERT INTO voiceagent_bookings (phone, cal_booking_uid, start_utc, topic) "
            "VALUES (%s, %s, %s, %s) ON CONFLICT (cal_booking_uid) DO NOTHING",
            (phone, uid, start_utc, topic),
        )

    async def list_upcoming(self, phone: str) -> list[dict[str, Any]]:
        cursor = await self._execute(
            "SELECT cal_booking_uid, start_utc, topic FROM voiceagent_bookings "
            "WHERE phone = %s AND status = 'confirmed' AND start_utc > now() "
            "ORDER BY start_utc",
            (phone,),
        )
        if cursor is None:
            return []
        return [
            {"uid": uid, "start_utc": start_utc, "topic": topic}
            for uid, start_utc, topic in await cursor.fetchall()
        ]

    async def set_status(self, uid: str, status: str) -> None:
        await self._execute(
            "UPDATE voiceagent_bookings SET status = %s WHERE cal_booking_uid = %s",
            (status, uid),
        )

    async def replace_uid(self, old_uid: str, new_uid: str, new_start_utc: dt.datetime) -> None:
        """Cal.com reschedule issues a NEW uid for the moved booking."""
        await self._execute(
            "UPDATE voiceagent_bookings SET cal_booking_uid = %s, start_utc = %s "
            "WHERE cal_booking_uid = %s",
            (new_uid, new_start_utc, old_uid),
        )


class SessionStore(_PgStore):
    """One row per call/chat session: outcome, transcript, latency, usage,
    cost and audit verdict — the source for /report and /costs."""

    _TABLE_SQL = (
        "CREATE TABLE IF NOT EXISTS voiceagent_sessions ("
        "id bigserial PRIMARY KEY, "
        "session_id text NOT NULL UNIQUE, "
        "phone text NOT NULL, "
        "channel text NOT NULL, "
        "brain text NOT NULL DEFAULT '', "
        "started_at timestamptz NOT NULL, "
        "ended_at timestamptz NOT NULL, "
        "duration_s double precision NOT NULL DEFAULT 0, "
        "user_turns int NOT NULL DEFAULT 0, "
        "assistant_turns int NOT NULL DEFAULT 0, "
        "outcome text NOT NULL DEFAULT 'no_action', "
        "actions jsonb NOT NULL DEFAULT '[]', "
        "turns jsonb NOT NULL DEFAULT '[]', "
        "latency jsonb NOT NULL DEFAULT '{}', "
        "usage jsonb NOT NULL DEFAULT '{}', "
        "cost_usd numeric(12,6) NOT NULL DEFAULT 0, "
        "cost_breakdown jsonb NOT NULL DEFAULT '{}', "
        "flags jsonb NOT NULL DEFAULT '[]', "
        "audit jsonb, "
        "audited_at timestamptz, "
        "created_at timestamptz NOT NULL DEFAULT now())",
        "CREATE INDEX IF NOT EXISTS idx_voiceagent_sessions_started "
        "ON voiceagent_sessions (started_at)",
        "CREATE INDEX IF NOT EXISTS idx_voiceagent_sessions_phone "
        "ON voiceagent_sessions (phone, started_at)",
    )

    _JSONB_FIELDS = ("actions", "turns", "latency", "usage", "cost_breakdown", "flags")

    async def record(self, row: dict[str, Any]) -> None:
        from psycopg.types.json import Jsonb

        params = dict(row)
        for field in self._JSONB_FIELDS:
            params[field] = Jsonb(params.get(field) or ([] if field in ("actions", "turns", "flags") else {}))
        await self._execute(
            "INSERT INTO voiceagent_sessions "
            "(session_id, phone, channel, brain, started_at, ended_at, duration_s, "
            "user_turns, assistant_turns, outcome, actions, turns, latency, usage, "
            "cost_usd, cost_breakdown, flags) VALUES "
            "(%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) "
            "ON CONFLICT (session_id) DO NOTHING",
            (
                params["session_id"], params["phone"], params["channel"],
                params.get("brain", ""), params["started_at"], params["ended_at"],
                params.get("duration_s", 0), params.get("user_turns", 0),
                params.get("assistant_turns", 0), params.get("outcome", "no_action"),
                params["actions"], params["turns"], params["latency"],
                params["usage"], params.get("cost_usd", 0), params["cost_breakdown"],
                params["flags"],
            ),
        )

    async def _fetch_dicts(self, sql: str, args: tuple[Any, ...]) -> list[dict[str, Any]]:
        cursor = await self._execute(sql, args)
        if cursor is None:
            return []
        columns = [d.name for d in cursor.description]
        rows = []
        for values in await cursor.fetchall():
            row = dict(zip(columns, values, strict=True))
            if "cost_usd" in row and row["cost_usd"] is not None:
                row["cost_usd"] = float(row["cost_usd"])
            rows.append(row)
        return rows

    async def sessions_since(self, since: dt.datetime) -> list[dict[str, Any]]:
        return await self._fetch_dicts(
            "SELECT session_id, phone, channel, brain, started_at, ended_at, "
            "duration_s, user_turns, assistant_turns, outcome, actions, latency, "
            "usage, cost_usd, cost_breakdown, flags, audit, audited_at "
            "FROM voiceagent_sessions WHERE started_at >= %s "
            "ORDER BY started_at DESC",
            (since,),
        )

    async def unaudited(self, since: dt.datetime, limit: int) -> list[dict[str, Any]]:
        """Flagged sessions first, then a random sample of the rest."""
        return await self._fetch_dicts(
            "SELECT session_id, channel, actions, turns, flags "
            "FROM voiceagent_sessions "
            "WHERE audit IS NULL AND started_at >= %s "
            "ORDER BY (jsonb_array_length(flags) > 0) DESC, random() LIMIT %s",
            (since, limit),
        )

    async def save_audit(self, session_id: str, verdict: dict[str, Any]) -> None:
        from psycopg.types.json import Jsonb

        await self._execute(
            "UPDATE voiceagent_sessions SET audit = %s, audited_at = now() "
            "WHERE session_id = %s",
            (Jsonb(verdict), session_id),
        )
