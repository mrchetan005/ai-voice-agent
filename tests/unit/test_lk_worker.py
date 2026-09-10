"""Worker: agent validation, discovery from env, server construction."""

from __future__ import annotations

from pathlib import Path

import pytest

pytest.importorskip("livekit.agents")

from voiceagent import VoiceAgent
from voiceagent.livekit.worker import (
    build_server,
    load_agents_from_settings,
    validate_agents,
)
from voiceagent.settings import SettingsError, load_settings

# for the VOICEAGENT_AGENTS module-path test
demo_agent = VoiceAgent(name="path-agent", llm="mock", stt="mock", tts="mock", system_prompt="x")


def _mock_agent(name: str = "m") -> VoiceAgent:
    return VoiceAgent(name=name, llm="mock", stt="mock", tts="mock", system_prompt="x")


def test_validate_rejects_empty() -> None:
    with pytest.raises(SettingsError, match="no agents"):
        validate_agents([])


def test_validate_rejects_duplicate_names() -> None:
    with pytest.raises(SettingsError, match="duplicate"):
        validate_agents([_mock_agent("a"), _mock_agent("a")])


def test_validate_lists_all_missing_provider_keys(monkeypatch: pytest.MonkeyPatch) -> None:
    for var in ("DEEPGRAM_API_KEY", "GOOGLE_API_KEY", "CARTESIA_API_KEY"):
        monkeypatch.delenv(var, raising=False)
    real = VoiceAgent(
        name="real", llm="google", stt="deepgram", tts="cartesia", system_prompt="x"
    )
    with pytest.raises(SettingsError) as exc:
        validate_agents([real])
    message = str(exc.value)
    assert "DEEPGRAM_API_KEY" in message
    assert "GOOGLE_API_KEY" in message
    assert "CARTESIA_API_KEY" in message


def test_validate_mock_agents_need_no_keys() -> None:
    validate_agents([_mock_agent()])


def test_load_agents_from_module_path(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[2]))
    monkeypatch.setenv("VOICEAGENT_AGENTS", "tests.unit.test_lk_worker:demo_agent")
    agents = load_agents_from_settings(load_settings())
    assert [a.name for a in agents] == ["path-agent"]


def test_load_agents_from_yaml_file(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    yaml_file = tmp_path / "agents.yaml"
    yaml_file.write_text(
        """
agents:
  - name: yaml-agent
    prompt: { text: hello }
    llm: { provider: mock }
    stt: { provider: mock }
    tts: { provider: mock }
""",
        encoding="utf-8",
    )
    monkeypatch.setenv("VOICEAGENT_AGENTS_FILE", str(yaml_file))
    agents = load_agents_from_settings(load_settings())
    assert [a.name for a in agents] == ["yaml-agent"]


def test_bad_module_path_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("VOICEAGENT_AGENTS", "no_colon_here")
    with pytest.raises(SettingsError, match=r"pkg\.module:attr"):
        load_agents_from_settings(load_settings())


def test_build_server_constructs(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LIVEKIT_API_KEY", "devkey")
    monkeypatch.setenv("LIVEKIT_API_SECRET", "s" * 36)
    server = build_server([_mock_agent()], load_settings())
    assert type(server).__name__ == "AgentServer"
