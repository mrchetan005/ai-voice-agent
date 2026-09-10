"""M0 smoke: the core package imports with no extras and settings validate."""

from __future__ import annotations

import pytest

import voiceagent
from voiceagent.settings import LiveKitSettings, SettingsError, load_settings


def test_package_imports() -> None:
    assert voiceagent.__version__


def test_settings_load_defaults(tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)  # no .env in cwd
    settings = load_settings()
    assert settings.livekit.url == "ws://localhost:7880"
    assert settings.worker.load_threshold == 0.7
    assert settings.recording.enabled is False


def test_settings_require_livekit_lists_all_missing(tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    with pytest.raises(SettingsError) as exc:
        load_settings(require_livekit=True)
    assert "LIVEKIT_API_KEY" in str(exc.value)
    assert "LIVEKIT_API_SECRET" in str(exc.value)


def test_settings_env_prefixes(tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("LIVEKIT_API_KEY", "k")
    monkeypatch.setenv("LIVEKIT_API_SECRET", "s")
    assert LiveKitSettings().require_credentials() == []
