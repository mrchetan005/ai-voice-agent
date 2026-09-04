"""OFFLINE checks for pricing.compute_cost (no keys, no network).

Run:  uv run tests/test_pricing.py
"""

from __future__ import annotations

import os
import sys

from whatsapp_agent.infra.pricing import PRICES, compute_cost, effective_prices


def main() -> int:
    failures: list[str] = []

    def check(name: str, ok: bool) -> None:
        print(f"  [{'PASS' if ok else 'FAIL'}] {name}")
        if not ok:
            failures.append(name)

    # Defaults ship with real rates (verified 2026-09-06) — math must match.
    rate = PRICES[("gemini_live", "prompt_tokens_audio")]
    result = compute_cost({"gemini_live": {"prompt_tokens_audio": 1_000_000}})
    check("default rates are non-zero and drive the math",
          rate > 0
          and abs(result["total_usd"] - rate * 1_000_000) < 1e-9
          and result["breakdown"]["gemini_live"]["units"]["prompt_tokens_audio"] == 1_000_000)
    check("Gemini Live audio-in default matches the $3/1M rate card",
          abs(rate * 1_000_000 - 3.00) < 1e-9)

    # Env override drives the math.
    os.environ["PRICE_GEMINI_LIVE_PROMPT_TOKENS_AUDIO"] = "0.000002"
    os.environ["WHATSAPP_MSG_COST_USD"] = "0.005"
    try:
        prices = effective_prices()
        check("env override applied",
              prices[("gemini_live", "prompt_tokens_audio")] == 0.000002
              and prices[("whatsapp", "messages")] == 0.005)
        result = compute_cost({
            "gemini_live": {"prompt_tokens_audio": 1_000_000},
            "whatsapp": {"messages": 4},
        })
        check("cost math: 1M audio tokens + 4 messages",
              abs(result["total_usd"] - (2.0 + 0.02)) < 1e-9)
        check("per-provider breakdown",
              abs(result["breakdown"]["whatsapp"]["usd"] - 0.02) < 1e-9)
    finally:
        del os.environ["PRICE_GEMINI_LIVE_PROMPT_TOKENS_AUDIO"]
        del os.environ["WHATSAPP_MSG_COST_USD"]

    # Unknown provider/unit never crashes and stays visible.
    result = compute_cost({"mystery": {"widgets": 7}, "gemini_live": {"bogus_unit": 3}})
    check("unknown pairs cost 0 but stay visible",
          result["total_usd"] == 0.0
          and result["breakdown"]["mystery"]["units"]["widgets"] == 7)

    # Garbage amounts are skipped, not fatal.
    result = compute_cost({"gemini_live": {"prompt_tokens": "not-a-number"}})
    check("non-numeric amount skipped", result["total_usd"] == 0.0)

    check("every PRICES key is a (provider, unit) tuple",
          all(isinstance(k, tuple) and len(k) == 2 for k in PRICES))

    print(f"\n{'ALL PASS' if not failures else f'{len(failures)} FAILURE(S): {failures}'}")
    return 0 if not failures else 1


if __name__ == "__main__":
    sys.exit(main())
