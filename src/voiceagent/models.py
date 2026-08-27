"""Data schemas for the voice agent proxy pipeline.

Everything that crosses a boundary (session config, orchestrator pulses,
approval round-trips, telemetry) is a validated pydantic model.  The single
exception is :class:`AudioFrame`, which sits on the audio hot path and is a
frozen ``dataclass`` with ``slots`` — pydantic validation per 20 ms frame
(~50 frames/sec/direction) would burn CPU for zero safety benefit, since
frames are produced only by our own transports.
"""

from __future__ import annotations

import os
import time
import uuid
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------


class SessionState(StrEnum):
    IDLE = "idle"
    LISTENING = "listening"
    THINKING = "thinking"
    SPEAKING = "speaking"
    AWAITING_APPROVAL = "awaiting_approval"
    CLOSED = "closed"


class ToolStepStatus(StrEnum):
    STARTED = "started"
    PROCESSING = "processing"
    COMPLETED = "completed"
    FAILED = "failed"
    AWAITING_APPROVAL = "awaiting_approval"


class ApprovalDecision(StrEnum):
    APPROVED = "approved"
    REJECTED = "rejected"
    TIMEOUT = "timeout"
    AMBIGUOUS = "ambiguous"


class GuardrailVerdict(StrEnum):
    ALLOW = "allow"
    MASK = "mask"  # text was sanitized in-place; forward the masked text
    BLOCK = "block"  # refuse; speak the profile's refusal line instead


# ---------------------------------------------------------------------------
# Session configuration
# ---------------------------------------------------------------------------


class SessionConfig(BaseModel):
    """Per-session runtime configuration.

    Constructed directly, from :meth:`from_env`, or from the ``VoiceAgent``
    facade kwargs.  Every provider receives the same config object.
    """

    session_id: str = Field(default_factory=lambda: uuid.uuid4().hex)
    language: str = "en-US"  # BCP-47; drives commentary templates + ASR/TTS
    tone: str = "neutral"  # persona tone: neutral | warm | formal | playful...
    system_prompt: str = "You are a helpful voice assistant."
    voice_id: str | None = None  # provider-specific voice; None = provider default
    guardrail_profile: str = "standard"  # off | standard | strict

    # Audio geometry. Providers override where the wire protocol is fixed
    # (OpenAI Realtime is 24 kHz-only; Gemini input is 16 kHz / output 24 kHz).
    input_sample_rate: int = 16_000
    output_sample_rate: int = 24_000

    # Interaction behaviour
    allow_barge_in: bool = True
    approval_timeout_s: float = 15.0
    # Coalescing window for commentary: pulses arriving faster than this are
    # merged so speech never stacks up behind a chatty orchestrator.
    commentary_min_gap_s: float = 2.5
    # Cancel the in-flight agent call when the user barges in.
    cancel_agent_on_barge_in: bool = False

    # Free-form provider knobs (model overrides, VAD tuning, endpoints...).
    provider_options: dict[str, Any] = Field(default_factory=dict)

    @classmethod
    def from_env(cls, prefix: str = "VOICEAGENT_") -> SessionConfig:
        """Build a config from ``VOICEAGENT_*`` environment variables.

        Only simple scalar fields are env-mappable; ``provider_options``
        accepts ``VOICEAGENT_OPT_<KEY>=<value>`` entries.
        """
        kwargs: dict[str, Any] = {}
        simple = {
            "LANGUAGE": "language",
            "TONE": "tone",
            "SYSTEM_PROMPT": "system_prompt",
            "VOICE_ID": "voice_id",
            "GUARDRAIL_PROFILE": "guardrail_profile",
        }
        for env_key, field_name in simple.items():
            if (val := os.environ.get(prefix + env_key)) is not None:
                kwargs[field_name] = val
        opts = {
            key.removeprefix(prefix + "OPT_").lower(): val
            for key, val in os.environ.items()
            if key.startswith(prefix + "OPT_")
        }
        if opts:
            kwargs["provider_options"] = opts
        return cls(**kwargs)


# ---------------------------------------------------------------------------
# Orchestrator contract: status pulses + approvals
# ---------------------------------------------------------------------------


class ToolStatusPulse(BaseModel):
    """The wire contract external orchestrators speak to this proxy.

    A backend agent emits these as JSON while it works, e.g.::

        {"status": "processing", "step": "fetching_db_records"}
        {"status": "awaiting_approval", "step": "delete_records",
         "requires_approval": true,
         "approval_payload": {"count": 45}}
    """

    pulse_id: str = Field(default_factory=lambda: uuid.uuid4().hex[:12])
    status: ToolStepStatus = ToolStepStatus.PROCESSING
    step: str
    detail: str | None = None
    requires_approval: bool = False
    approval_payload: dict[str, Any] | None = None
    ts: float = Field(default_factory=time.time)


class ApprovalRequest(BaseModel):
    request_id: str = Field(default_factory=lambda: uuid.uuid4().hex[:12])
    step: str
    prompt_text: str  # what the voice engine speaks to the user
    payload: dict[str, Any] = Field(default_factory=dict)
    timeout_s: float = 15.0


class ApprovalResult(BaseModel):
    request_id: str
    decision: ApprovalDecision
    transcript: str | None = None  # what the user actually said
    latency_ms: float = 0.0

    def to_upstream(self) -> dict[str, Any]:
        """Clean JSON execution payload forwarded to the orchestrator."""
        return {
            "type": "approval_result",
            "request_id": self.request_id,
            "approved": self.decision is ApprovalDecision.APPROVED,
            "decision": self.decision.value,
            "transcript": self.transcript,
        }


# ---------------------------------------------------------------------------
# Audio hot path
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class AudioFrame:
    """Zero-copy audio carrier: ``data`` is the original ``bytes`` object
    received from the socket/mic — never sliced or re-encoded in the proxy."""

    data: bytes
    sample_rate: int
    ts_monotonic: float = field(default_factory=time.monotonic)
    channels: int = 1

    @property
    def duration_ms(self) -> float:
        # 16-bit PCM assumed throughout the pipeline (2 bytes/sample).
        return len(self.data) / (self.sample_rate * self.channels * 2) * 1000.0


# ---------------------------------------------------------------------------
# Telemetry
# ---------------------------------------------------------------------------


class LatencyMetrics(BaseModel):
    """Aggregated per-session latency snapshot (milliseconds)."""

    model_config = ConfigDict(extra="allow")

    session_id: str
    handshake_ms: float | None = None
    asr_latency_ms: list[float] = Field(default_factory=list)
    llm_ttfb_ms: list[float] = Field(default_factory=list)
    tts_first_byte_ms: list[float] = Field(default_factory=list)
    network_rtt_ms: list[float] = Field(default_factory=list)

    @staticmethod
    def _summary(values: list[float]) -> dict[str, float]:
        if not values:
            return {"count": 0}
        ordered = sorted(values)
        p95_idx = max(0, int(len(ordered) * 0.95) - 1)
        return {
            "count": len(ordered),
            "avg": sum(ordered) / len(ordered),
            "p50": ordered[len(ordered) // 2],
            "p95": ordered[p95_idx],
            "max": ordered[-1],
        }

    def report(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "handshake_ms": self.handshake_ms,
            "asr_latency_ms": self._summary(self.asr_latency_ms),
            "llm_ttfb_ms": self._summary(self.llm_ttfb_ms),
            "tts_first_byte_ms": self._summary(self.tts_first_byte_ms),
            "network_rtt_ms": self._summary(self.network_rtt_ms),
        }


class PipelineEvent(BaseModel):
    """One structured log record; serialized as a single JSON line."""

    ts: float = Field(default_factory=time.time)
    session_id: str
    kind: str  # e.g. "asr.final", "approval.result", "guardrail.block"
    data: dict[str, Any] = Field(default_factory=dict)
    severity: Literal["debug", "info", "warning", "error"] = "info"
