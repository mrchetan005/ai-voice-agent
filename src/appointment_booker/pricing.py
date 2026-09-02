"""Per-unit provider rates and cost computation.

Rates below were taken from the provider pricing pages on 2026-09-06
(source noted per block). Providers reprice without notice — re-verify
periodically or pin your own numbers via PRICE_* env overrides. Raw
quantities are always recorded, so past sessions can be re-priced
retroactively from the sessions table's `usage` column.

Units are what the meters count: tokens are per SINGLE token (the
provider's per-1M rate divided by 1_000_000), audio per second, TTS per
character, WhatsApp per message.
"""

from __future__ import annotations

import logging
import os
from typing import Any

logger = logging.getLogger("appointment_booker")

PRICES: dict[tuple[str, str], float] = {
    # Gemini Live — gemini-3.1-flash-live-preview, paid tier
    # (ai.google.dev/gemini-api/docs/pricing, 2026-09-06): text in
    # $0.75/1M, audio in $3.00/1M, text out $4.50/1M, audio out $12.00/1M.
    # NOTE: the Live API re-bills the accumulated session context every
    # turn, so prompt tokens grow with call length — real billing, not a bug.
    ("gemini_live", "prompt_tokens_text"): 0.75 / 1e6,
    ("gemini_live", "prompt_tokens_audio"): 3.00 / 1e6,
    ("gemini_live", "response_tokens_text"): 4.50 / 1e6,
    ("gemini_live", "response_tokens_audio"): 12.00 / 1e6,
    # Gemini Flash — gemini-3.6-flash standard tier (same page):
    # $0.75/1M in, $3.75/1M out THROUGH 2026-12-31; doubles to
    # $1.50/$7.50 on 2027-01-01 — update these then.
    ("gemini_flash", "input_tokens"): 0.75 / 1e6,
    ("gemini_flash", "output_tokens"): 3.75 / 1e6,
    ("gemini_flash_recap", "input_tokens"): 0.75 / 1e6,
    ("gemini_flash_recap", "output_tokens"): 3.75 / 1e6,
    # OpenAI Realtime — gpt-realtime-2.1 (openai pricing page, 2026-09-06):
    # text in $4/1M, audio in $32/1M, text out $24/1M, audio out $64/1M.
    # Totals (input_tokens/output_tokens) stay unpriced — only the
    # text/audio splits are billed, same pattern as gemini_live.
    ("openai_realtime", "input_tokens_text"): 4.00 / 1e6,
    ("openai_realtime", "input_tokens_audio"): 32.00 / 1e6,
    ("openai_realtime", "output_tokens_text"): 24.00 / 1e6,
    ("openai_realtime", "output_tokens_audio"): 64.00 / 1e6,
    # Groq — openai/gpt-oss-20b (groq.com/pricing, 2026-09-06):
    # $0.075/1M in, $0.30/1M out.
    ("groq", "input_tokens"): 0.075 / 1e6,
    ("groq", "output_tokens"): 0.30 / 1e6,
    # OpenAI as the BRAIN llm — rates below are gpt-4o-mini ($0.15/$0.60
    # per 1M, 2026-09-06); if you pick a different model, override via
    # PRICE_OPENAI_INPUT_TOKENS / PRICE_OPENAI_OUTPUT_TOKENS.
    ("openai", "input_tokens"): 0.15 / 1e6,
    ("openai", "output_tokens"): 0.60 / 1e6,
    # OpenRouter / custom gateways (LiteLLM etc): per-model pricing varies —
    # set PRICE_OPENROUTER_* / PRICE_CUSTOM_LLM_* to your model's rate.
    # Token quantities are always recorded either way; OpenRouter's own
    # dashboard shows exact spend.
    ("openrouter", "input_tokens"): 0.0,
    ("openrouter", "output_tokens"): 0.0,
    ("custom_llm", "input_tokens"): 0.0,
    ("custom_llm", "output_tokens"): 0.0,
    # Deepgram — nova-3 monolingual STREAMING at the regular $0.0077/min
    # (deepgram.com/pricing, 2026-09-06; a limited-time promo runs at
    # $0.0048/min — budget at the regular rate).
    ("deepgram", "audio_seconds"): 0.0077 / 60,
    # Cartesia — ~1 credit/char; effective $/char is PLAN-dependent,
    # roughly $5-$37 per 1M chars (cartesia.ai/pricing, 2026-09-06).
    # Default is the conservative ~$37/1M; set PRICE_CARTESIA_CHARACTERS
    # to your plan's real effective rate.
    ("cartesia", "characters"): 37.0 / 1e6,
    # WhatsApp India: service/utility messages inside the 24h window are
    # FREE until 2026-09-30; from 2026-10-01 Meta charges ₹0.115 + 18%
    # GST ≈ $0.0016/message. Defaulting to the forward rate; set
    # WHATSAPP_MSG_COST_USD=0 before Oct 2026 for exactness.
    ("whatsapp", "messages"): 0.0016,
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
