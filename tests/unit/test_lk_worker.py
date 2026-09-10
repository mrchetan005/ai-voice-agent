"""Worker: agent validation, discovery from env, server construction."""

from __future__ import annotations

from pathlib import Path

import pytest

pytest.importorskip("livekit.agents")

from voiceagent import VoiceAgent
from voiceagent.agent import load_agents
from voiceagent.livekit.worker import (
    build_server,
    missing_keys_by_agent,
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


def test_missing_keys_reported_per_agent(monkeypatch: pytest.MonkeyPatch) -> None:
    for var in ("DEEPGRAM_API_KEY", "GOOGLE_API_KEY", "CARTESIA_API_KEY"):
        monkeypatch.delenv(var, raising=False)
    real = VoiceAgent(
        name="real", llm="google", stt="deepgram", tts="cartesia", system_prompt="x"
    )
    missing = missing_keys_by_agent([real, _mock_agent()])
    assert set(missing) == {"real"}
    assert missing["real"] == ["CARTESIA_API_KEY", "DEEPGRAM_API_KEY", "GOOGLE_API_KEY"]


def test_build_server_skips_keyless_agents_but_serves_rest(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("LIVEKIT_API_KEY", "devkey")
    monkeypatch.setenv("LIVEKIT_API_SECRET", "s" * 36)
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    real = VoiceAgent(
        name="real", llm="google", stt="deepgram", tts="cartesia", system_prompt="x"
    )
    server = build_server([real, _mock_agent()], load_settings())
    assert type(server).__name__ == "AgentServer"


def test_build_server_fails_when_nothing_servable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    monkeypatch.setenv("LIVEKIT_API_KEY", "devkey")
    monkeypatch.setenv("LIVEKIT_API_SECRET", "s" * 36)
    real = VoiceAgent(
        name="real", llm="google", stt="deepgram", tts="cartesia", system_prompt="x"
    )
    with pytest.raises(SettingsError, match="no servable agents"):
        build_server([real], load_settings())


def test_validate_mock_agents_need_no_keys() -> None:
    validate_agents([_mock_agent()])


def test_load_agents_from_module_path(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[2]))
    monkeypatch.setenv("VOICEAGENT_AGENTS", "tests.unit.test_lk_worker:demo_agent")
    agents = load_agents(load_settings())
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
    agents = load_agents(load_settings())
    assert [a.name for a in agents] == ["yaml-agent"]


def test_bad_module_path_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("VOICEAGENT_AGENTS", "no_colon_here")
    with pytest.raises(SettingsError, match=r"pkg\.module:attr"):
        load_agents(load_settings())


def test_build_server_constructs(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LIVEKIT_API_KEY", "devkey")
    monkeypatch.setenv("LIVEKIT_API_SECRET", "s" * 36)
    server = build_server([_mock_agent()], load_settings())
    assert type(server).__name__ == "AgentServer"
