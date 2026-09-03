"""Input guardrails: prompt-injection blocking, PII masking, profanity policy."""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field

from .models import (
    GuardrailVerdict,
)

logger = logging.getLogger("voiceagent")

# ---------------------------------------------------------------------------
# Guardrails
# ---------------------------------------------------------------------------


@dataclass
class GuardrailOutcome:
    verdict: GuardrailVerdict
    text: str
    refusal_text: str | None = None
    triggered: list[str] = field(default_factory=list)


_REFUSAL_LINES = {
    "en": "I can't help with that request.",
    "hi": "माफ़ कीजिए, मैं इस अनुरोध में मदद नहीं कर सकता।",
    "es": "No puedo ayudar con esa solicitud.",
}

# Prompt-injection heuristics. Regex is a first line of defense, not a
# moderation model: it catches the common copy-pasted jailbreak shapes at
# zero latency; profiles can layer a remote moderator behind it.
_INJECTION_PATTERNS = [
    re.compile(r"ignore\s+(?:all\s+|any\s+)?(?:previous|prior|above)\s+(?:instructions|prompts|rules)", re.IGNORECASE),
    re.compile(r"disregard\s+(?:the\s+)?(?:system|previous)\s+(?:prompt|instructions)", re.IGNORECASE),
    re.compile(r"reveal\s+(?:your\s+)?(?:system\s+prompt|hidden\s+instructions)", re.IGNORECASE),
    re.compile(r"\b(?:jailbreak|developer\s+mode|dan\s+mode)\b", re.IGNORECASE),
    re.compile(r"act\s+as\s+if\s+you\s+have\s+no\s+(?:rules|restrictions|guardrails)", re.IGNORECASE),
    re.compile(r"[A-Za-z0-9+/]{80,}={0,2}"),  # long base64 blob smuggling
]

_PROFANITY = re.compile(r"\b(?:fuck|shit|bitch|asshole|bastard)\b", re.IGNORECASE)

_EMAIL = re.compile(r"[\w.+-]+@[\w-]+\.[\w.]+")
_SSN = re.compile(r"\b\d{3}-\d{2}-\d{4}\b")
_ID12 = re.compile(r"\b\d{4}[\s-]\d{4}[\s-]\d{4}\b")  # aadhaar-style grouped id
_CARDISH = re.compile(r"\b(?:\d[ -]?){13,19}\b")
_PHONE = re.compile(r"(?<!\d)\+?\d[\d\s-]{8,14}\d(?!\d)")


def _luhn_ok(digits: str) -> bool:
    total = 0
    for i, ch in enumerate(reversed(digits)):
        d = int(ch)
        if i % 2 == 1:
            d *= 2
            if d > 9:
                d -= 9
        total += d
    return total % 10 == 0


def mask_pii(text: str) -> tuple[str, list[str]]:
    """Replace PII spans with typed placeholders; returns (masked, hits)."""
    hits: list[str] = []

    def sub(pattern: re.Pattern[str], placeholder: str, source: str) -> str:
        def repl(match: re.Match[str]) -> str:
            hits.append(placeholder)
            return placeholder

        return pattern.sub(repl, source)

    # Order matters: cards before phones (a card number IS 13-19 digits).
    def card_repl(match: re.Match[str]) -> str:
        digits = re.sub(r"\D", "", match.group())
        if 13 <= len(digits) <= 19 and _luhn_ok(digits):
            hits.append("[CARD]")
            return "[CARD]"
        return match.group()

    text = _CARDISH.sub(card_repl, text)
    text = sub(_EMAIL, "[EMAIL]", text)
    text = sub(_SSN, "[SSN]", text)
    text = sub(_ID12, "[ID]", text)
    text = sub(_PHONE, "[PHONE]", text)
    return text, hits


class GuardrailPipeline:
    """Ordered input checks run on every user transcript BEFORE it reaches
    the agent/orchestrator.

    Profiles:
      * ``off``      — passthrough (trusted internal setups).
      * ``standard`` — block injection, mask PII, mask profanity.
      * ``strict``   — standard + BLOCK on card numbers and profanity.
    """

    def __init__(self, profile: str = "standard", language: str = "en-US") -> None:
        self.profile = profile
        self._lang = language.split("-")[0].lower()

    def _refusal(self) -> str:
        return _REFUSAL_LINES.get(self._lang, _REFUSAL_LINES["en"])

    async def check(self, text: str) -> GuardrailOutcome:
        # async signature so a remote moderation call can slot in per
        # profile without changing any caller; the built-ins are pure CPU
        # and add microseconds, never a network round trip.
        if self.profile == "off":
            return GuardrailOutcome(GuardrailVerdict.ALLOW, text)

        triggered: list[str] = []
        for pattern in _INJECTION_PATTERNS:
            if pattern.search(text):
                triggered.append(f"injection:{pattern.pattern[:30]}")
                return GuardrailOutcome(
                    GuardrailVerdict.BLOCK, text, self._refusal(), triggered
                )

        masked, pii_hits = mask_pii(text)
        triggered.extend(pii_hits)
        if self.profile == "strict" and "[CARD]" in pii_hits:
            return GuardrailOutcome(
                GuardrailVerdict.BLOCK, masked, self._refusal(), triggered
            )

        if _PROFANITY.search(masked):
            triggered.append("profanity")
            if self.profile == "strict":
                return GuardrailOutcome(
                    GuardrailVerdict.BLOCK, masked, self._refusal(), triggered
                )
            masked = _PROFANITY.sub("***", masked)

        if triggered:
            return GuardrailOutcome(GuardrailVerdict.MASK, masked, None, triggered)
        return GuardrailOutcome(GuardrailVerdict.ALLOW, text)

