"""Tests for observability cost table, env overrides, and usage collection."""

from __future__ import annotations

from voiceagent.observability.cost import PRICES, compute_cost, effective_prices
from voiceagent.observability.usage import UsageCollector


def test_compute_cost_prices_known_units() -> None:
    result = compute_cost({"deepgram": {"audio_seconds": 60.0}})
    assert result["breakdown"]["deepgram"]["usd"] == round(0.0077, 6)
    assert result["total_usd"] == round(0.0077, 6)


def test_compute_cost_unknown_units_visible_but_free() -> None:
    result = compute_cost({"mock": {"widgets": 5}})
    assert result["total_usd"] == 0.0
    assert result["breakdown"]["mock"]["units"] == {"widgets": 5.0}


def test_compute_cost_never_crashes_on_garbage_amounts() -> None:
    result = compute_cost({"deepgram": {"audio_seconds": "nan-ish", "ok": 1}})  # type: ignore[dict-item]
    assert "deepgram" in result["breakdown"]
    assert "audio_seconds" not in result["breakdown"]["deepgram"]["units"]


def test_env_override_with_hyphenated_provider(monkeypatch) -> None:
    monkeypatch.setenv("PRICE_GOOGLE_REALTIME_INPUT_TOKENS_AUDIO", "0.5")
    prices = effective_prices()
    assert prices[("google-realtime", "input_tokens_audio")] == 0.5
    # untouched rows keep their defaults
    assert prices[("deepgram", "audio_seconds")] == PRICES[("deepgram", "audio_seconds")]


def test_env_override_non_numeric_ignored(monkeypatch) -> None:
    monkeypatch.setenv("PRICE_DEEPGRAM_AUDIO_SECONDS", "not-a-number")
    assert effective_prices()[("deepgram", "audio_seconds")] == PRICES[
        ("deepgram", "audio_seconds")
    ]


def test_usage_collector_merge_accumulates_and_finalizes() -> None:
    collector = UsageCollector()
    collector.merge({"deepgram": {"audio_seconds": 10}})
    collector.merge({"deepgram": {"audio_seconds": 5}, "cartesia": {"characters": 100}})
    usage, cost = collector.finalize()
    assert usage["deepgram"]["audio_seconds"] == 15.0
    assert usage["cartesia"]["characters"] == 100.0
    assert cost["total_usd"] > 0


def test_usage_collector_replace_overwrites_snapshot() -> None:
    collector = UsageCollector()
    collector.replace({"openai": {"input_tokens": 100}})
    collector.replace({"openai": {"input_tokens": 250}})  # cumulative snapshot re-sent
    assert collector.usage["openai"]["input_tokens"] == 250.0


def test_usage_collector_ignores_garbage() -> None:
    collector = UsageCollector()
    collector.add("x", "y", "zzz")  # type: ignore[arg-type]
    assert collector.usage == {}
