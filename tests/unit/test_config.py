"""AgentConfig validation, defaults, and YAML loading."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from voiceagent.config import (
    AgentConfig,
    MemoryConfig,
    STTConfig,
    TTSConfig,
    load_agents_yaml,
)

PROMPT = {"text": "You are a helpful agent."}


def pipeline_dict(**overrides: Any) -> dict[str, Any]:
    data: dict[str, Any] = {
        "name": "sales-agent-2",
        "prompt": PROMPT,
        "llm": {"provider": "openai"},
        "stt": {},
        "tts": {},
    }
    data.update(overrides)
    return data


def realtime_dict(**overrides: Any) -> dict[str, Any]:
    data: dict[str, Any] = {
        "name": "rt-agent",
        "mode": "realtime",
        "prompt": PROMPT,
        "realtime": {"provider": "openai"},
    }
    data.update(overrides)
    return data


# --- mode consistency ---------------------------------------------------


def test_pipeline_mode_valid() -> None:
    cfg = AgentConfig.from_dict(pipeline_dict())
    assert cfg.mode == "pipeline"
    assert cfg.realtime is None


@pytest.mark.parametrize("missing", ["llm", "stt", "tts"])
def test_pipeline_mode_requires_llm_stt_tts(missing: str) -> None:
    data = pipeline_dict()
    del data[missing]
    with pytest.raises(ValidationError, match="pipeline mode requires"):
        AgentConfig.from_dict(data)


def test_pipeline_mode_rejects_realtime_config() -> None:
    with pytest.raises(ValidationError, match="does not take a 'realtime'"):
        AgentConfig.from_dict(pipeline_dict(realtime={"provider": "openai"}))


def test_realtime_mode_valid() -> None:
    cfg = AgentConfig.from_dict(realtime_dict())
    assert cfg.mode == "realtime"
    assert cfg.llm is None and cfg.stt is None and cfg.tts is None


def test_realtime_mode_requires_realtime_config() -> None:
    data = realtime_dict()
    del data["realtime"]
    with pytest.raises(ValidationError, match="requires a 'realtime' config"):
        AgentConfig.from_dict(data)


@pytest.mark.parametrize(
    "extra",
    [
        {"llm": {"provider": "openai"}},
        {"stt": {}},
        {"tts": {}},
    ],
)
def test_realtime_mode_rejects_pipeline_configs(extra: dict[str, Any]) -> None:
    with pytest.raises(ValidationError, match="does not take llm/stt/tts"):
        AgentConfig.from_dict(realtime_dict(**extra))


# --- name validation ----------------------------------------------------


@pytest.mark.parametrize("bad", ["Sales-Agent", "sales_agent", "-sales", ""])
def test_name_rejects_invalid(bad: str) -> None:
    with pytest.raises(ValidationError):
        AgentConfig.from_dict(pipeline_dict(name=bad))


def test_name_accepts_lowercase_hyphen_digits() -> None:
    cfg = AgentConfig.from_dict(pipeline_dict(name="sales-agent-2"))
    assert cfg.name == "sales-agent-2"


# --- defaults -----------------------------------------------------------


def test_defaults() -> None:
    cfg = AgentConfig.from_dict(pipeline_dict())
    assert cfg.stt is not None and cfg.stt.provider == "deepgram"
    assert cfg.tts is not None and cfg.tts.provider == "cartesia"
    assert cfg.turn_detection == "vad"
    assert cfg.max_tool_steps == 5
    assert cfg.language == "en"
    assert cfg.allow_interruptions is True
    assert cfg.tools == []
    assert cfg.memory is None


def test_stt_tts_provider_defaults() -> None:
    assert STTConfig().provider == "deepgram"
    assert TTSConfig().provider == "cartesia"


def test_memory_config_defaults() -> None:
    mem = MemoryConfig()
    assert mem.backend == "inmemory"
    assert mem.url is None
    assert mem.max_messages == 50
    assert mem.ttl_s == 604_800


# --- from_dict roundtrip ------------------------------------------------


def test_from_dict_roundtrip() -> None:
    cfg = AgentConfig.from_dict(
        pipeline_dict(
            greeting="hi",
            memory={"backend": "redis", "max_messages": 10},
            tools=["pkg.mod:tool"],
            metadata={"team": "sales"},
        )
    )
    assert AgentConfig.from_dict(cfg.model_dump()) == cfg


# --- YAML loading -------------------------------------------------------

SINGLE_AGENT_YAML = """\
name: sales-agent-2
prompt:
  text: "Hello {caller}"
  variables:
    caller: friend
llm:
  provider: openai
  model: gpt-4o-mini
stt: {}
tts:
  voice: nova
"""

MULTI_AGENT_YAML = """\
agents:
  - name: agent-one
    prompt: {text: one}
    llm: {provider: openai}
    stt: {}
    tts: {}
  - name: agent-two
    mode: realtime
    prompt: {text: two}
    realtime: {provider: openai}
"""


def test_from_yaml_single_agent(tmp_path: Path) -> None:
    path = tmp_path / "agent.yaml"
    path.write_text(SINGLE_AGENT_YAML, encoding="utf-8")
    cfg = AgentConfig.from_yaml(path)
    assert cfg.name == "sales-agent-2"
    assert cfg.llm is not None and cfg.llm.model == "gpt-4o-mini"
    assert cfg.tts is not None and cfg.tts.voice == "nova"
    assert cfg.prompt.resolve().render() == "Hello friend"


def test_from_yaml_rejects_multi_agent_file(tmp_path: Path) -> None:
    path = tmp_path / "agents.yaml"
    path.write_text(MULTI_AGENT_YAML, encoding="utf-8")
    with pytest.raises(ValueError, match="load_agents_yaml"):
        AgentConfig.from_yaml(path)


def test_load_agents_yaml_list(tmp_path: Path) -> None:
    path = tmp_path / "agents.yaml"
    path.write_text(MULTI_AGENT_YAML, encoding="utf-8")
    configs = load_agents_yaml(path)
    assert [c.name for c in configs] == ["agent-one", "agent-two"]
    assert configs[1].mode == "realtime"


def test_load_agents_yaml_single_mapping(tmp_path: Path) -> None:
    path = tmp_path / "agent.yaml"
    path.write_text(SINGLE_AGENT_YAML, encoding="utf-8")
    configs = load_agents_yaml(path)
    assert len(configs) == 1
    assert configs[0].name == "sales-agent-2"


def test_load_agents_yaml_duplicate_names(tmp_path: Path) -> None:
    path = tmp_path / "agents.yaml"
    path.write_text(
        MULTI_AGENT_YAML.replace("agent-two", "agent-one"),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="duplicate agent names"):
        load_agents_yaml(path)
