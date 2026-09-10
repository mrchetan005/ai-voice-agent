"""Shared test configuration.

Default test runs are 100% offline: no provider keys, no network, no docker.
- `-m live` tests hit real provider APIs (frugal — credits are limited).
- `-m integration` tests need the local compose stack (still $0).
"""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep settings-affecting env vars from leaking into tests."""
    for var in (
        "LIVEKIT_URL",
        "LIVEKIT_API_KEY",
        "LIVEKIT_API_SECRET",
        "PLATFORM_API_TOKEN",
        "PLATFORM_DATABASE_URL",
        "PLATFORM_REDIS_URL",
        "VOICEAGENT_AGENTS",
        "VOICEAGENT_AGENTS_FILE",
        "RECORDING_ENABLED",
    ):
        monkeypatch.delenv(var, raising=False)
