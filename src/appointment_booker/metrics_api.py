"""Read-only observability endpoints served by the webhook server.

  GET /costs?days=N   (or ?since=<ISO8601>)  — per-session costs + totals,
                      the machine contract for a consumer agent
  GET /report?days=N  — health: volumes, outcomes, conversion, pooled
                      latency percentiles, flags, audit verdicts, costs

Auth: `Authorization: Bearer $METRICS_TOKEN`. The server is publicly
exposed (ngrok/Meta webhooks), so with METRICS_TOKEN unset the endpoints
answer 404 — indistinguishable from not existing. Wrong token -> 401.
"""

from __future__ import annotations

import datetime as dt
import logging
from typing import Any

from aiohttp import web

from appointment_booker.stores import SessionStore

logger = logging.getLogger("appointment_booker")

_MAX_DAYS = 366


def _percentiles(values: list[float]) -> dict[str, float | int]:
    if not values:
        return {"count": 0}
    ordered = sorted(values)
    p95_idx = max(0, int(len(ordered) * 0.95) - 1)
    return {
        "count": len(ordered),
        "p50": round(ordered[len(ordered) // 2], 1),
        "p95": round(ordered[p95_idx], 1),
    }


def _iso(value: Any) -> Any:
    return value.isoformat() if isinstance(value, dt.datetime) else value


class MetricsAPI:
    def __init__(self) -> None:
        self._session_store: SessionStore | None = None

    def _auth(self, request: web.Request) -> web.Response | None:
        from appointment_booker.config import get_settings

        token = get_settings().metrics_token
        if not token:
            return web.json_response({"error": "not found"}, status=404)
        if request.headers.get("Authorization") != f"Bearer {token}":
            return web.json_response({"error": "unauthorized"}, status=401)
        return None

    async def _store(self) -> SessionStore | None:
        # Lazy + cached-only-when-connected, so a DB blip retries next call.
        from appointment_booker.config import get_settings

        if self._session_store is not None:
            return self._session_store
        db_url = get_settings().database_url
        if not db_url:
            return None
        store = SessionStore(db_url)
        await store.connect()
        if store.degraded:
            return None
        self._session_store = store
        return store

    @staticmethod
    def _window(request: web.Request, default_days: int) -> tuple[dt.datetime, dict[str, Any]]:
        until = dt.datetime.now(dt.UTC)
        if raw_since := request.query.get("since"):
            try:
                since = dt.datetime.fromisoformat(raw_since)
                if since.tzinfo is None:
                    since = since.replace(tzinfo=dt.UTC)
                days = max(1, round((until - since).total_seconds() / 86400))
                return since, {"since": _iso(since), "until": _iso(until), "days": days}
            except ValueError:
                pass  # fall through to days
        try:
            days = int(request.query.get("days", default_days))
        except ValueError:
            days = default_days
        days = min(max(days, 1), _MAX_DAYS)
        since = until - dt.timedelta(days=days)
        return since, {"since": _iso(since), "until": _iso(until), "days": days}

    # -- GET /costs ---------------------------------------------------------

    async def handle_costs(self, request: web.Request) -> web.Response:
        if denied := self._auth(request):
            return denied
        store = await self._store()
        if store is None:
            return web.json_response({"error": "database unavailable"}, status=503)
        since, window = self._window(request, default_days=30)
        rows = await store.sessions_since(since)

        sessions = []
        by_provider: dict[str, float] = {}
        by_channel: dict[str, float] = {}
        total = 0.0
        for row in rows:
            cost = float(row.get("cost_usd") or 0)
            total += cost
            by_channel[row["channel"]] = by_channel.get(row["channel"], 0.0) + cost
            for provider, detail in (row.get("cost_breakdown") or {}).items():
                by_provider[provider] = by_provider.get(provider, 0.0) + float(
                    (detail or {}).get("usd", 0)
                )
            sessions.append({
                "session_id": row["session_id"],
                "phone": row["phone"],
                "channel": row["channel"],
                "brain": row.get("brain", ""),
                "started_at": _iso(row["started_at"]),
                "duration_s": row.get("duration_s", 0),
                "outcome": row.get("outcome", ""),
                "cost_usd": cost,
                "cost_breakdown": row.get("cost_breakdown") or {},
            })
        return web.json_response({
            "generated_at": _iso(dt.datetime.now(dt.UTC)),
            "window": window,
            "currency": "USD",
            "sessions": sessions,
            "totals": {
                "sessions": len(sessions),
                "cost_usd": round(total, 6),
                "by_provider": {k: round(v, 6) for k, v in by_provider.items()},
                "by_channel": {k: round(v, 6) for k, v in by_channel.items()},
                "avg_cost_per_session_usd": round(total / len(sessions), 6) if sessions else 0.0,
            },
        })

    # -- GET /report ---------------------------------------------------------

    async def handle_report(self, request: web.Request) -> web.Response:
        if denied := self._auth(request):
            return denied
        store = await self._store()
        if store is None:
            return web.json_response({"error": "database unavailable"}, status=503)
        since, window = self._window(request, default_days=7)
        rows = await store.sessions_since(since)

        by_channel: dict[str, int] = {}
        by_day: dict[str, int] = {}
        outcomes: dict[str, int] = {}
        flag_counts: dict[str, int] = {}
        pooled: dict[str, list[float]] = {}
        by_provider: dict[str, float] = {}
        total_cost = 0.0
        audited_rows = []
        for row in rows:
            by_channel[row["channel"]] = by_channel.get(row["channel"], 0) + 1
            day = _iso(row["started_at"])[:10]
            by_day[day] = by_day.get(day, 0) + 1
            outcome = row.get("outcome", "no_action")
            outcomes[outcome] = outcomes.get(outcome, 0) + 1
            for flag in row.get("flags") or []:
                flag_counts[flag] = flag_counts.get(flag, 0) + 1
            for metric, values in ((row.get("latency") or {}).get("raw") or {}).items():
                pooled.setdefault(metric.removesuffix("_ms"), []).extend(values)
            total_cost += float(row.get("cost_usd") or 0)
            for provider, detail in (row.get("cost_breakdown") or {}).items():
                by_provider[provider] = by_provider.get(provider, 0.0) + float(
                    (detail or {}).get("usd", 0)
                )
            if row.get("audit") is not None:
                audited_rows.append(row)

        total = len(rows)
        scores = [
            row["audit"]["score"] for row in audited_rows
            if isinstance(row["audit"], dict) and isinstance(row["audit"].get("score"), (int, float))
        ]
        latest_audits = [
            {"session_id": row["session_id"],
             "score": row["audit"].get("score"),
             "summary": row["audit"].get("summary", row["audit"].get("error", ""))}
            for row in audited_rows[:5]
            if isinstance(row["audit"], dict)
        ]
        return web.json_response({
            "generated_at": _iso(dt.datetime.now(dt.UTC)),
            "window": window,
            "volume": {"total_sessions": total, "by_channel": by_channel,
                       "by_day": dict(sorted(by_day.items()))},
            "outcomes": outcomes,
            "conversion": {
                "booked_rate": round(outcomes.get("booked", 0) / total, 3) if total else 0.0,
                "drop_off_rate": round(flag_counts.get("short_session", 0) / total, 3) if total else 0.0,
            },
            "latency_ms": {metric: _percentiles(values) for metric, values in pooled.items()},
            "health": {
                "flag_counts": flag_counts,
                "error_sessions": outcomes.get("error", 0),
                "degraded": flag_counts.get("db_degraded", 0) > 0,
            },
            "audit": {
                "audited": len(audited_rows),
                "unaudited": total - len(audited_rows),
                "avg_score": round(sum(scores) / len(scores), 2) if scores else None,
                "below_7": sum(1 for s in scores if s < 7),
                "latest": latest_audits,
            },
            "cost": {
                "total_usd": round(total_cost, 6),
                "by_provider": {k: round(v, 6) for k, v in by_provider.items()},
                "avg_per_session_usd": round(total_cost / total, 6) if total else 0.0,
            },
        })
