"""Per-unit provider rates and cost computation.

╔══════════════════════════════════════════════════════════════════════╗
║  OPERATOR ACTION REQUIRED: every rate below ships as 0.0.             ║
║  Fill in real USD-per-unit prices from the provider pricing pages     ║
║  (or set PRICE_* env overrides) or every cost will read $0.00:        ║
║    Gemini:   https://ai.google.dev/pricing                            ║
║    Deepgram: https://deepgram.com/pricing                             ║
║    Groq:     https://groq.com/pricing                                 ║
║    Cartesia: https://cartesia.ai/pricing                              ║
║    WhatsApp: https://business.whatsapp.com/products/platform-pricing  ║
║  Raw quantities are always recorded, so past sessions can be          ║
║  re-priced retroactively from the sessions table's `usage` column.    ║
╚══════════════════════════════════════════════════════════════════════╝

Units are what the meters count: tokens are per SINGLE token (divide the
provider's per-1M rate by 1_000_000), audio per second, TTS per character,
WhatsApp per message.
"""

from __future__ import annotations

import logging
import os
from typing import Any

logger = logging.getLogger("appointment_booker")

PRICES: dict[tuple[str, str], float] = {
    # Gemini Live (voice model, single- and dual-brain calls)
    ("gemini_live", "prompt_tokens_text"): 0.0,
    ("gemini_live", "prompt_tokens_audio"): 0.0,
    ("gemini_live", "response_tokens_text"): 0.0,
    ("gemini_live", "response_tokens_audio"): 0.0,
    # Gemini Flash (dual-brain/chat booking brain + hallucination judge)
    ("gemini_flash", "input_tokens"): 0.0,
    ("gemini_flash", "output_tokens"): 0.0,
    # Gemini Flash for the post-call recap (tracked separately)
    ("gemini_flash_recap", "input_tokens"): 0.0,
    ("gemini_flash_recap", "output_tokens"): 0.0,
    # Split-stack providers
    ("groq", "input_tokens"): 0.0,
    ("groq", "output_tokens"): 0.0,
    ("deepgram", "audio_seconds"): 0.0,
    ("cartesia", "characters"): 0.0,
    # Meta bills per conversation window; this is a per-message estimate
    # (0 inside the 24h service window). Override: WHATSAPP_MSG_COST_USD.
    ("whatsapp", "messages"): 0.0,
}


def effective_prices() -> dict[tuple[str, str], float]:
    """PRICES with env overrides applied: PRICE_<PROVIDER>_<UNIT>=<usd>
    (e.g. PRICE_GEMINI_LIVE_PROMPT_TOKENS_AUDIO=0.0000021), plus the
    WHATSAPP_MSG_COST_USD shorthand."""
    prices = dict(PRICES)
    for (provider, unit) in PRICES:
        env_key = f"PRICE_{provider.upper()}_{unit.upper()}"
        raw = os.environ.get(env_key)
        if raw:
            try:
                prices[(provider, unit)] = float(raw)
            except ValueError:
                logger.warning("ignoring non-numeric %s=%r", env_key, raw)
    if raw := os.environ.get("WHATSAPP_MSG_COST_USD"):
        try:
            prices[("whatsapp", "messages")] = float(raw)
        except ValueError:
            logger.warning("ignoring non-numeric WHATSAPP_MSG_COST_USD=%r", raw)
    return prices


def compute_cost(usage: dict[str, dict[str, float]]) -> dict[str, Any]:
    """{provider: {unit: amount}} -> {"total_usd", "breakdown"}.

    Unknown (provider, unit) pairs cost 0 but stay visible in the units so
    unmetered spend is never silently hidden — costing must never crash.
    """
    prices = effective_prices()
    total = 0.0
    breakdown: dict[str, Any] = {}
    for provider, units in usage.items():
        provider_usd = 0.0
        clean_units: dict[str, float] = {}
        for unit, amount in units.items():
            try:
                amount = float(amount)
            except (TypeError, ValueError):
                continue
            clean_units[unit] = round(amount, 4)
            provider_usd += amount * prices.get((provider, unit), 0.0)
        breakdown[provider] = {"usd": round(provider_usd, 6), "units": clean_units}
        total += provider_usd
    return {"total_usd": round(total, 6), "breakdown": breakdown}
