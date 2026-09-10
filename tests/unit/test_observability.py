"""Metrics hooks and tracer-provider construction (offline)."""

from __future__ import annotations

import pytest

pytest.importorskip("prometheus_client")

from voiceagent.events import (
    SessionEnded,
    SessionIDs,
    SessionStarted,
    ToolCallFinished,
)
from voiceagent.observability import build_tracer_provider, metrics
from voiceagent.settings import ObservabilitySettings


def _ids(agent: str, channel: str = "browser") -> SessionIDs:
    return SessionIDs(agent_id=agent, channel=channel, session_id="s1", room_id="r1")


def test_session_and_tool_counters_increment() -> None:
    ids = _ids("obs-unit-agent")
    metrics.on_event(SessionStarted(ids=ids))
    metrics.on_event(SessionStarted(ids=ids))
    metrics.on_event(SessionEnded(ids=ids, reason="completed", duration_s=12.5))
    metrics.on_event(ToolCallFinished(ids=ids, tool="book", result="ok"))
    metrics.on_event(ToolCallFinished(ids=ids, tool="book", error="boom"))

    text = metrics.render_latest()[0].decode()
    assert 'va_sessions_started_total{agent_id="obs-unit-agent",channel="browser"} 2.0' in text
    assert 'va_sessions_ended_total{agent_id="obs-unit-agent",channel="browser",reason="completed"} 1.0' in text
    assert 'va_tool_calls_total{agent_id="obs-unit-agent",status="ok",tool="book"} 1.0' in text
    assert 'va_tool_calls_total{agent_id="obs-unit-agent",status="error",tool="book"} 1.0' in text


def test_record_cost_and_usage() -> None:
    metrics.record_cost(
        "obs-cost-agent", {"deepgram": {"audio_seconds": 3.6}}, 0.0123
    )
    text = metrics.render_latest()[0].decode()
    assert 'va_usage_units_total{provider="deepgram",unit="audio_seconds"} 3.6' in text
    assert 'va_cost_usd_total{agent_id="obs-cost-agent"} 0.0123' in text


def test_session_created_counter() -> None:
    metrics.session_created("obs-created-agent", "browser")
    metrics.session_created("obs-created-agent", "browser")
    text = metrics.render_latest()[0].decode()
    assert 'va_api_sessions_created_total{agent_id="obs-created-agent",channel="browser"} 2.0' in text


def test_render_latest_content_type() -> None:
    _, content_type = metrics.render_latest()
    assert "text/plain" in content_type


def test_tracer_provider_disabled_without_endpoint() -> None:
    assert build_tracer_provider(ObservabilitySettings(otel_exporter_otlp_endpoint="")) is None


def test_tracer_provider_built_with_endpoint() -> None:
    provider = build_tracer_provider(
        ObservabilitySettings(
            otel_exporter_otlp_endpoint="http://localhost:4318/v1/traces",
            otel_service_name="voiceagent-test",
        )
    )
    assert provider is not None
    assert hasattr(provider, "add_span_processor")
