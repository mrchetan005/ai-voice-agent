"""Prometheus metrics for the worker and platform.

Metrics live on the default prometheus_client registry. In the worker,
livekit-agents already serves ``/metrics`` with a ``MultiProcessCollector``
(the image sets ``PROMETHEUS_MULTIPROC_DIR``), so counters incremented inside
job subprocesses aggregate onto the worker's port automatically. The platform
renders the same way via :func:`render_latest`.

If prometheus_client is not installed (a core-only install), every hook here
is a no-op — observability is optional, never load-bearing.
"""

from __future__ import annotations

import logging
import os

from voiceagent.events import BaseEvent, SessionEnded, SessionStarted, ToolCallFinished

logger = logging.getLogger("voiceagent")

try:
    from prometheus_client import (
        CONTENT_TYPE_LATEST,
        CollectorRegistry,
        Counter,
        Histogram,
        generate_latest,
        multiprocess,
    )

    _ENABLED = True
except ImportError:  # pragma: no cover - obs extra not installed
    _ENABLED = False
    CONTENT_TYPE_LATEST = "text/plain; version=0.0.4; charset=utf-8"


if _ENABLED:
    SESSIONS_STARTED = Counter(
        "va_sessions_started_total", "Sessions started", ["agent_id", "channel"]
    )
    SESSIONS_ENDED = Counter(
        "va_sessions_ended_total", "Sessions ended", ["agent_id", "channel", "reason"]
    )
    SESSION_DURATION = Histogram(
        "va_session_duration_seconds",
        "Session wall-clock duration",
        ["agent_id", "channel"],
        buckets=(1, 5, 15, 30, 60, 120, 300, 600, 1800),
    )
    TOOL_CALLS = Counter(
        "va_tool_calls_total", "Tool invocations", ["agent_id", "tool", "status"]
    )
    USAGE_UNITS = Counter(
        "va_usage_units_total", "Provider usage units consumed", ["provider", "unit"]
    )
    COST_USD = Counter(
        "va_cost_usd_total", "Estimated provider cost in USD", ["agent_id"]
    )
    # Control-plane side: tokens minted. Distinct from va_sessions_started_total
    # (worker: agent actually joined) — not every created session connects.
    API_SESSIONS_CREATED = Counter(
        "va_api_sessions_created_total",
        "Sessions created by the control plane",
        ["agent_id", "channel"],
    )


def on_event(event: BaseEvent) -> None:
    """Built-in event handler: translate canonical events into metrics.

    Attach alongside the user's handlers on the worker's emit dispatcher.
    """
    if not _ENABLED:
        return
    ids = event.ids
    if isinstance(event, SessionStarted):
        SESSIONS_STARTED.labels(ids.agent_id, ids.channel).inc()
    elif isinstance(event, SessionEnded):
        SESSIONS_ENDED.labels(ids.agent_id, ids.channel, event.reason or "completed").inc()
        SESSION_DURATION.labels(ids.agent_id, ids.channel).observe(event.duration_s)
    elif isinstance(event, ToolCallFinished):
        TOOL_CALLS.labels(ids.agent_id, event.tool, "error" if event.error else "ok").inc()


def session_created(agent_id: str, channel: str) -> None:
    """Control-plane hook: a session token was minted."""
    if not _ENABLED:
        return
    API_SESSIONS_CREATED.labels(agent_id, channel).inc()


def record_cost(agent_id: str, usage: dict[str, dict[str, float]], cost_usd: float) -> None:
    """Record a session's final usage quantities and priced cost (call once)."""
    if not _ENABLED:
        return
    for provider, units in usage.items():
        for unit, amount in units.items():
            if amount:
                USAGE_UNITS.labels(provider, unit).inc(amount)
    if cost_usd:
        COST_USD.labels(agent_id).inc(cost_usd)


def render_latest() -> tuple[bytes, str]:
    """(body, content-type) for a ``/metrics`` endpoint.

    Mirrors livekit-agents: aggregate across processes when multiprocess mode
    is on, else serve the default registry.
    """
    if not _ENABLED:
        return b"", CONTENT_TYPE_LATEST
    if "PROMETHEUS_MULTIPROC_DIR" in os.environ:
        registry = CollectorRegistry(auto_describe=True)
        multiprocess.MultiProcessCollector(registry)  # type: ignore[no-untyped-call]
        return generate_latest(registry), CONTENT_TYPE_LATEST
    return generate_latest(), CONTENT_TYPE_LATEST


__all__ = [
    "CONTENT_TYPE_LATEST",
    "on_event",
    "record_cost",
    "render_latest",
    "session_created",
]
