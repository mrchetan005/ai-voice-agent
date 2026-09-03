"""Compatibility shim: the original module split into guardrails.py,
telemetry.py and eval_harness.py — every historical import keeps working.

Run the offline harness::

    uv run python -m voiceagent.guardrails_and_eval
"""

from __future__ import annotations

from .eval_harness import (  # noqa: F401
    FluencyEvaluator,
    LLMJudge,
    MockTransport,
    _wait_until,
    run_eval,
)
from .guardrails import (  # noqa: F401
    GuardrailOutcome,
    GuardrailPipeline,
    mask_pii,
)
from .telemetry import Span, TelemetryRecorder  # noqa: F401

if __name__ == "__main__":
    import asyncio

    raise SystemExit(asyncio.run(run_eval()))
