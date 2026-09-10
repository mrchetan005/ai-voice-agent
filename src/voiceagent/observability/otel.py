"""OpenTelemetry tracing setup (engine-neutral).

``build_tracer_provider`` returns an OTLP-exporting ``TracerProvider`` (or
``None`` when tracing is not configured / OTel is not installed). The livekit
layer is what actually hands it to ``livekit.agents.telemetry`` — keeping this
module free of any engine import, per the one-way layering rule.
"""

from __future__ import annotations

import logging
from typing import Any

from voiceagent.settings import ObservabilitySettings

logger = logging.getLogger("voiceagent")


def build_tracer_provider(
    obs: ObservabilitySettings, *, service_name: str | None = None
) -> Any | None:
    """Build an OTLP ``TracerProvider``; ``None`` if disabled or unavailable.

    Never raises — tracing is optional and must not break startup.
    """
    endpoint = obs.otel_exporter_otlp_endpoint
    if not endpoint:
        return None

    try:
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor

        if obs.otel_exporter_otlp_protocol == "grpc":
            from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import (
                OTLPSpanExporter,
            )
        else:
            from opentelemetry.exporter.otlp.proto.http.trace_exporter import (
                OTLPSpanExporter,
            )
    except ImportError:
        logger.warning(
            "OTEL_EXPORTER_OTLP_ENDPOINT set but opentelemetry is not installed "
            "(pip install 'voiceagent[obs]') — tracing disabled"
        )
        return None

    name = service_name or obs.otel_service_name
    provider = TracerProvider(resource=Resource.create({"service.name": name}))
    provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter(endpoint=endpoint)))
    logger.info("otel tracing enabled: %s (%s)", endpoint, obs.otel_exporter_otlp_protocol)
    return provider


__all__ = ["build_tracer_provider"]
