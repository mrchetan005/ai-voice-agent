"""Latency telemetry: metric spans, recorder, structured pipeline events."""

from __future__ import annotations

import logging
import time
from typing import Any, ClassVar

from .models import (
    LatencyMetrics,
    PipelineEvent,
)

logger = logging.getLogger("voiceagent")
# ---------------------------------------------------------------------------
# Telemetry
# ---------------------------------------------------------------------------


class Span:
    """Monotonic-clock timing span; ``end()`` records into the metric bucket."""

    __slots__ = ("_name", "_recorder", "_start")

    def __init__(self, recorder: TelemetryRecorder, name: str) -> None:
        self._recorder = recorder
        self._name = name
        self._start = time.monotonic()

    def end(self) -> float:
        ms = (time.monotonic() - self._start) * 1000.0
        self._recorder.record(self._name, ms)
        return ms


class TelemetryRecorder:
    """Collects latency metrics and emits structured JSON log lines."""

    _BUCKETS: ClassVar[dict[str, str]] = {
        "asr_latency": "asr_latency_ms",
        "llm_ttfb": "llm_ttfb_ms",
        "agent.turn": "agent_turn_ms",
        "tts_first_byte": "tts_first_byte_ms",
        "network_rtt": "network_rtt_ms",
        "e2e_response": "e2e_response_ms",
    }

    def __init__(self, session_id: str, emit_logs: bool = True) -> None:
        self.metrics = LatencyMetrics(session_id=session_id)
        self._emit = emit_logs

    def record(self, name: str, ms: float) -> None:
        if name == "handshake":
            self.metrics.handshake_ms = ms
        elif (bucket := self._BUCKETS.get(name)) is not None:
            getattr(self.metrics, bucket).append(ms)
        self.log_event(f"metric.{name}", {"ms": round(ms, 2)}, severity="debug")

    def span(self, name: str) -> Span:
        return Span(self, name)

    def log_event(
        self, kind: str, data: dict[str, Any], severity: str = "info"
    ) -> None:
        if not self._emit:
            return
        event = PipelineEvent(
            session_id=self.metrics.session_id, kind=kind, data=data,
            severity=severity,  # type: ignore[arg-type]
        )
        line = event.model_dump_json()
        if severity == "debug":
            logger.debug(line)
        elif severity == "warning":
            logger.warning(line)
        elif severity == "error":
            logger.error(line)
        else:
            logger.info(line)

    def report(self) -> dict[str, Any]:
        return self.metrics.report()

