"""Shared test configuration.

Default test runs are 100% offline: no provider keys, no network, no docker.
- `-m live` tests hit real provider APIs (frugal — credits are limited).
- `-m integration` tests need the local compose stack (still $0).

Every test runs in a temp CWD so a developer's repo-root `.env` (used by the
compose stack) can never leak into settings, and env vars that feed
pydantic-settings are cleared.
"""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _isolated_env(monkeypatch: pytest.MonkeyPatch, tmp_path_factory: pytest.TempPathFactory,
                  request: pytest.FixtureRequest) -> None:
    if request.node.get_closest_marker("integration") is None:
        monkeypatch.chdir(tmp_path_factory.mktemp("cwd"))
    for var in (
        "LIVEKIT_URL",
        "LIVEKIT_PUBLIC_URL",
        "LIVEKIT_API_KEY",
        "LIVEKIT_API_SECRET",
        "PLATFORM_API_TOKEN",
        "PLATFORM_DATABASE_URL",
        "PLATFORM_REDIS_URL",
        "PLATFORM_SIP_TRUNK_ID",
        "VOICEAGENT_AGENTS",
        "VOICEAGENT_AGENTS_FILE",
        "VOICEAGENT_WORKER_NAME",
        "RECORDING_ENABLED",
    ):
        monkeypatch.delenv(var, raising=False)
