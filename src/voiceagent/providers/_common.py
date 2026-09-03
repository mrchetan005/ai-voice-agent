"""Shared helpers for the provider engines."""

from __future__ import annotations

import os

from ..base import BaseVoiceAgentProxy


def _require_env(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise RuntimeError(f"Missing required environment variable {name}")
    return value


def _record(proxy: BaseVoiceAgentProxy, metric: str, ms: float) -> None:
    if proxy.telemetry is not None:
        proxy.telemetry.record(metric, ms)
