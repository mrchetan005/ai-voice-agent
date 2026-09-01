"""Per-session usage metering and session-record assembly.

Hot-path rule: meters only do in-memory addition and never raise — the one
database write happens once at teardown via write_session_record, wrapped
so a failure can only cost us the record, never the call.
"""

from __future__ import annotations

import datetime as dt
import logging
import uuid
from typing import Any

from appointment_booker.audit import deterministic_flags
from appointment_booker.pricing import compute_cost
from appointment_booker.stores import SessionStore

logger = logging.getLogger("appointment_booker")


class UsageMeter:
    def __init__(self, session_id: str, phone: str, channel: str, brain: str = "") -> None:
        self.session_id = session_id
        self.phone = phone
        self.channel = channel
        self.brain = brain
        self._usage: dict[str, dict[str, float]] = {}

    def add(self, provider: str, unit: str, amount: float | int | None) -> None:
        try:
            value = float(amount or 0)
        except (TypeError, ValueError):
            return
        if value == 0:
            return
        self._usage.setdefault(provider, {})
        self._usage[provider][unit] = self._usage[provider].get(unit, 0.0) + value

    def merge_gemini_live(self, proxy_usage: dict[str, float]) -> None:
        """Snapshot GeminiLiveProxy.usage at teardown."""
        for key, value in (proxy_usage or {}).items():
            self.add("gemini_live", key, value)

    def merge_split_stack(self, proxy_usage: dict[str, float]) -> None:
        """Snapshot SplitStackProxy.usage at teardown."""
        mapping = {
            "asr_audio_seconds": ("deepgram", "audio_seconds"),
            "llm_input_tokens": ("groq", "input_tokens"),
            "llm_output_tokens": ("groq", "output_tokens"),
            "tts_characters": ("cartesia", "characters"),
        }
        for key, value in (proxy_usage or {}).items():
            provider, unit = mapping.get(key, ("split_stack", key))
            self.add(provider, unit, value)

    def snapshot(self) -> dict[str, dict[str, float]]:
        return {provider: dict(units) for provider, units in self._usage.items()}


def derive_outcome(actions: list[dict[str, str]], errors: list[dict[str, str]]) -> str:
    kinds = {action.get("action") for action in actions}
    if "booked" in kinds:
        return "booked"
    if "rescheduled" in kinds:
        return "rescheduled"
    if "cancelled" in kinds:
        return "cancelled"
    if any(err.get("error") for err in errors):
        return "error"
    return "no_action"


def _latency_payload(telemetry: Any) -> dict[str, Any]:
    """Summary + raw ms lists (capped) — raw lists let /report pool
    percentiles across sessions, which per-session summaries can't."""
    if telemetry is None:
        return {}
    metrics = telemetry.metrics
    raw = {}
    for field in ("asr_latency_ms", "llm_ttfb_ms", "tts_first_byte_ms",
                  "network_rtt_ms", "agent_turn_ms", "e2e_response_ms"):
        values = getattr(metrics, field, None) or []
        if values:
            raw[field] = [round(v, 1) for v in values[:500]]
    return {"report": telemetry.report(), "raw": raw}


async def write_session_record(
    store: SessionStore,
    meter: UsageMeter,
    *,
    turns: list[tuple[str, str]],
    actions: list[dict[str, str]],
    errors: list[dict[str, str]],
    telemetry: Any,
    started_at: dt.datetime,
    db_degraded: bool,
    language: str = "",
) -> None:
    """Assemble and persist one voiceagent_sessions row. Never raises."""
    try:
        ended_at = dt.datetime.now(dt.UTC)
        duration_s = max(0.0, (ended_at - started_at).total_seconds())
        usage = meter.snapshot()
        cost = compute_cost(usage)
        await store.record({
            "session_id": meter.session_id,
            "phone": meter.phone,
            "channel": meter.channel,
            "brain": meter.brain,
            "started_at": started_at,
            "ended_at": ended_at,
            "duration_s": duration_s,
            "user_turns": sum(1 for role, _ in turns if role == "user"),
            "assistant_turns": sum(1 for role, _ in turns if role == "assistant"),
            "outcome": derive_outcome(actions, errors),
            "actions": actions,
            "turns": [[role, text] for role, text in turns],
            "latency": _latency_payload(telemetry),
            "usage": usage,
            "cost_usd": cost["total_usd"],
            "cost_breakdown": cost["breakdown"],
            "flags": deterministic_flags(
                turns, actions, errors,
                db_degraded=db_degraded, duration_s=duration_s, language=language,
            ),
        })
    except Exception:
        logger.exception("session record write failed (record lost, call unaffected)")


class ChatSessionTracker:
    """Per-sender chat session: a >30 min silence gap starts a new session
    (flushed lazily on the next message or at chat-loop shutdown)."""

    GAP = dt.timedelta(minutes=30)

    def __init__(self, phone: str) -> None:
        self.phone = phone
        self.meter: UsageMeter = None  # type: ignore[assignment]  # set by reset()
        self.reset()

    def reset(self) -> None:
        now = dt.datetime.now(dt.UTC)
        self.started_at = now
        self.last_at = now
        self.turns: list[tuple[str, str]] = []
        self.meter = UsageMeter(uuid.uuid4().hex, self.phone, "chat")

    def stale(self, now: dt.datetime | None = None) -> bool:
        return ((now or dt.datetime.now(dt.UTC)) - self.last_at) > self.GAP

    def touch(self) -> None:
        self.last_at = dt.datetime.now(dt.UTC)
