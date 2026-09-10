"""Per-unit provider rates and cost computation.

Ported from the original app's pricing module (rates re-checked against
provider pricing pages on 2026-09-06; source noted per block). Providers
reprice without notice — re-verify periodically or pin your own numbers via
``PRICE_<PROVIDER>_<UNIT>`` env overrides. Raw quantities are always recorded
with the session, so past sessions can be re-priced retroactively.

Units are what the meters count: tokens are per SINGLE token (the provider's
per-1M rate divided by 1_000_000), STT audio per second, TTS per character.
Provider keys match the registry names, with ``-realtime`` suffixed for
realtime models (their token economics differ from the plain LLM APIs).
"""

from __future__ import annotations

import logging
import os
from typing import Any

logger = logging.getLogger("voiceagent")

PRICES: dict[tuple[str, str], float] = {
    # Google Gemini flash, standard tier (ai.google.dev/gemini-api/docs/pricing,
    # 2026-09-06): $0.75/1M in, $3.75/1M out THROUGH 2026-12-31; doubles to
    # $1.50/$7.50 on 2027-01-01 — update these then.
    ("google", "input_tokens"): 0.75 / 1e6,
    ("google", "output_tokens"): 3.75 / 1e6,
    # Gemini Live (realtime; same page): text in $0.75/1M, audio in $3.00/1M,
    # text out $4.50/1M, audio out $12.00/1M. NOTE: the Live API re-bills the
    # accumulated session context every turn, so prompt tokens grow with call
    # length — real billing, not a bug.
    ("google-realtime", "input_tokens_text"): 0.75 / 1e6,
    ("google-realtime", "input_tokens_audio"): 3.00 / 1e6,
    ("google-realtime", "output_tokens_text"): 4.50 / 1e6,
    ("google-realtime", "output_tokens_audio"): 12.00 / 1e6,
    # OpenAI as the pipeline LLM — defaults are gpt-4o-mini ($0.15/$0.60 per
    # 1M, 2026-09-06); different model => override via PRICE_OPENAI_*.
    ("openai", "input_tokens"): 0.15 / 1e6,
    ("openai", "output_tokens"): 0.60 / 1e6,
    # OpenAI Realtime — gpt-realtime (openai pricing page, 2026-09-06):
    # text in $4/1M, audio in $32/1M, text out $24/1M, audio out $64/1M.
    # Totals stay unpriced — only the text/audio splits are billed.
    ("openai-realtime", "input_tokens_text"): 4.00 / 1e6,
    ("openai-realtime", "input_tokens_audio"): 32.00 / 1e6,
    ("openai-realtime", "output_tokens_text"): 24.00 / 1e6,
    ("openai-realtime", "output_tokens_audio"): 64.00 / 1e6,
    # Deepgram — nova-3 monolingual STREAMING at the regular $0.0077/min
    # (deepgram.com/pricing, 2026-09-06).
    ("deepgram", "audio_seconds"): 0.0077 / 60,
    # Cartesia — ~1 credit/char; effective $/char is PLAN-dependent, roughly
    # $5-$37 per 1M chars (cartesia.ai/pricing, 2026-09-06). Conservative
    # default; set PRICE_CARTESIA_CHARACTERS to your plan's effective rate.
    ("cartesia", "characters"): 37.0 / 1e6,
    # ElevenLabs — credit-based, plan-dependent; unpriced by default so the
    # quantity stays visible. Set PRICE_ELEVENLABS_CHARACTERS for your plan.
    ("elevenlabs", "characters"): 0.0,
}


def effective_prices() -> dict[tuple[str, str], float]:
    """PRICES with env overrides applied: ``PRICE_<PROVIDER>_<UNIT>=<usd>``.

    Hyphens in provider names map to underscores in the env key, e.g.
    ``PRICE_GOOGLE_REALTIME_INPUT_TOKENS_AUDIO=0.0000021``.
    """
    prices = dict(PRICES)
    for provider, unit in PRICES:
        env_key = f"PRICE_{provider.replace('-', '_').upper()}_{unit.upper()}"
        raw = os.environ.get(env_key)
        if raw:
            try:
                prices[(provider, unit)] = float(raw)
            except ValueError:
                logger.warning("ignoring non-numeric %s=%r", env_key, raw)
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
