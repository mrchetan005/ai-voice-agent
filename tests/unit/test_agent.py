"""VoiceAgent sugar API: kwargs -> AgentConfig, tools, events, from_config."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from voiceagent.agent import VoiceAgent, resolve_tool_path
from voiceagent.config import AgentConfig, LLMConfig, STTConfig, TTSConfig
from voiceagent.events import BaseEvent
from voiceagent.prompts import PromptConfig, PromptTemplate
from voiceagent.tools import Tool, tool

REPO_ROOT = Path(__file__).resolve().parents[2]


@tool
def sample_tool(city: str) -> str:
    """Report the weather for a city."""
    return f"sunny in {city}"


def plain_tool_fn(city: str) -> str:
    """Report the weather for a city."""
    return f"rainy in {city}"


# --- pipeline sugar ---------------------------------------------------------


def test_pipeline_kwargs_map_to_config() -> None:
    agent = VoiceAgent(
        "receptionist",
        llm="google",
        model="gemini-2.0-flash",
        temperature=0.3,
        llm_options={"top_p": 0.9},
        stt="deepgram:nova-3",
        tts="cartesia:sonic-2",
        voice="warm-lady",
        language="hi",
        system_prompt="You are helpful.",
    )
    cfg = agent.config
    assert cfg.mode == "pipeline"
    assert cfg.name == "receptionist"
    assert agent.name == "receptionist"
    assert cfg.llm is not None
    assert cfg.llm.provider == "google"
    assert cfg.llm.model == "gemini-2.0-flash"
    assert cfg.llm.temperature == 0.3
    assert cfg.llm.options == {"top_p": 0.9}
    assert cfg.stt is not None
    assert cfg.stt.provider == "deepgram"
    assert cfg.stt.model == "nova-3"
    assert cfg.stt.language == "hi"
    assert cfg.tts is not None
    assert cfg.tts.provider == "cartesia"
    assert cfg.tts.model == "sonic-2"
    assert cfg.tts.voice == "warm-lady"
    assert cfg.tts.language == "hi"
    assert cfg.realtime is None


def test_pipeline_defaults_deepgram_cartesia() -> None:
    agent = VoiceAgent("basic", llm="openai", system_prompt="hi")
    cfg = agent.config
    assert cfg.stt is not None
    assert cfg.stt.provider == "deepgram"
    assert cfg.stt.model is None
    assert cfg.tts is not None
    assert cfg.tts.provider == "cartesia"
    assert cfg.tts.model is None
    assert cfg.tts.voice is None
    assert cfg.llm is not None
    assert cfg.llm.temperature is None
    assert cfg.llm.options == {}
    assert cfg.language == "en"


def test_pipeline_missing_llm_raises() -> None:
    with pytest.raises(ValueError, match="pipeline mode needs llm"):
        VoiceAgent("basic", system_prompt="hi")


# --- realtime sugar ---------------------------------------------------------


def test_realtime_llm_becomes_realtime_provider() -> None:
    agent = VoiceAgent(
        "live",
        mode="realtime",
        llm="google",
        model="gemini-2.0-flash-live",
        voice="Puck",
        llm_options={"modalities": ["audio"]},
        system_prompt="hi",
    )
    cfg = agent.config
    assert cfg.mode == "realtime"
    assert cfg.realtime is not None
    assert cfg.realtime.provider == "google"
    assert cfg.realtime.model == "gemini-2.0-flash-live"
    assert cfg.realtime.voice == "Puck"
    assert cfg.realtime.options == {"modalities": ["audio"]}
    assert cfg.llm is None
    assert cfg.stt is None
    assert cfg.tts is None


def test_realtime_rejects_stt_and_tts() -> None:
    with pytest.raises(ValueError, match="does not take stt/tts"):
        VoiceAgent("live", mode="realtime", llm="google", stt="deepgram", system_prompt="hi")
    with pytest.raises(ValueError, match="does not take stt/tts"):
        VoiceAgent("live", mode="realtime", llm="google", tts="cartesia", system_prompt="hi")


def test_realtime_missing_llm_raises() -> None:
    with pytest.raises(ValueError, match="realtime mode needs llm"):
        VoiceAgent("live", mode="realtime", system_prompt="hi")


# --- prompt normalization ---------------------------------------------------


def test_missing_system_prompt_raises() -> None:
    with pytest.raises(ValueError, match="system_prompt"):
        VoiceAgent("basic", llm="google")


def test_system_prompt_template_normalizes() -> None:
    tpl = PromptTemplate("Hello {name}.", {"name": "Ada"})
    agent = VoiceAgent("basic", llm="google", system_prompt=tpl)
    assert agent.config.prompt.text == "Hello {name}."
    assert agent.config.prompt.variables == {"name": "Ada"}
    assert agent.config.prompt.file is None


def test_system_prompt_config_passes_through() -> None:
    pc = PromptConfig(text="Be brief.", variables={"tone": "calm"})
    agent = VoiceAgent("basic", llm="google", system_prompt=pc)
    assert agent.config.prompt.text == "Be brief."
    assert agent.config.prompt.variables == {"tone": "calm"}


# --- tools ------------------------------------------------------------------


def test_callables_auto_wrapped_and_tools_passed_through() -> None:
    def lookup_order(order_id: str) -> str:
        """Find an order by its id."""
        return order_id

    agent = VoiceAgent(
        "basic", llm="google", system_prompt="hi", tools=[lookup_order, sample_tool]
    )
    assert all(isinstance(t, Tool) for t in agent.tools)
    assert agent.tools[0].name == "lookup_order"
    assert agent.tools[0].description == "Find an order by its id."
    assert agent.tools[1] is sample_tool


def test_add_tool_wraps_and_returns() -> None:
    agent = VoiceAgent("basic", llm="google", system_prompt="hi")

    def ping() -> str:
        """Check liveness."""
        return "pong"

    wrapped = agent.add_tool(ping)
    assert isinstance(wrapped, Tool)
    assert wrapped.name == "ping"
    assert agent.add_tool(sample_tool) is sample_tool
    assert agent.tools == [wrapped, sample_tool]


# --- events -----------------------------------------------------------------


def test_on_event_registers_and_returns_handler() -> None:
    agent = VoiceAgent("basic", llm="google", system_prompt="hi")
    assert agent.event_handlers == ()

    @agent.on_event
    async def on_anything(event: BaseEvent) -> None:
        pass

    def sync_handler(event: BaseEvent) -> None:
        pass

    assert agent.on_event(sync_handler) is sync_handler
    assert on_anything is not None  # decorator returned the function itself
    assert agent.event_handlers == (on_anything, sync_handler)
    assert isinstance(agent.event_handlers, tuple)


# --- from_config ------------------------------------------------------------


def _pipeline_config(**overrides: object) -> AgentConfig:
    data: dict[str, object] = {
        "name": "cfg-agent",
        "prompt": PromptConfig(text="hi"),
        "llm": LLMConfig(provider="google"),
        "stt": STTConfig(),
        "tts": TTSConfig(),
    }
    data.update(overrides)
    return AgentConfig(**data)  # type: ignore[arg-type]


def test_from_config_agentconfig() -> None:
    cfg = _pipeline_config()
    agent = VoiceAgent.from_config(cfg)
    assert agent.config is cfg
    assert agent.name == "cfg-agent"


def test_from_config_dict() -> None:
    agent = VoiceAgent.from_config(
        {
            "name": "dict-agent",
            "mode": "realtime",
            "prompt": {"text": "hi"},
            "realtime": {"provider": "google", "voice": "Puck"},
        }
    )
    assert agent.name == "dict-agent"
    assert agent.config.mode == "realtime"
    assert agent.config.realtime is not None
    assert agent.config.realtime.voice == "Puck"


def test_from_config_yaml_path(tmp_path: Path) -> None:
    data = {
        "name": "yaml-agent",
        "prompt": {"text": "You are concise."},
        "llm": {"provider": "google", "model": "gemini-2.0-flash"},
        "stt": {"provider": "deepgram", "model": "nova-3"},
        "tts": {"provider": "cartesia", "voice": "warm-lady"},
        "greeting": "hello",
    }
    path = tmp_path / "agent.yaml"
    path.write_text(yaml.safe_dump(data), encoding="utf-8")
    for source in (path, str(path)):
        agent = VoiceAgent.from_config(source)
        assert agent.name == "yaml-agent"
        assert agent.config.llm is not None
        assert agent.config.llm.model == "gemini-2.0-flash"
        assert agent.config.tts is not None
        assert agent.config.tts.voice == "warm-lady"
        assert agent.config.greeting == "hello"


def test_config_tool_paths_resolved(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.syspath_prepend(str(REPO_ROOT))
    cfg = _pipeline_config(tools=["tests.unit.test_agent:sample_tool"])
    agent = VoiceAgent("cfg-agent", config=cfg)
    assert len(agent.tools) == 1
    assert isinstance(agent.tools[0], Tool)
    assert agent.tools[0].name == "sample_tool"
    assert agent.tools[0].description == "Report the weather for a city."


# --- resolve_tool_path ------------------------------------------------------


def test_resolve_tool_path_tool_and_callable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.syspath_prepend(str(REPO_ROOT))
    resolved = resolve_tool_path("tests.unit.test_agent:sample_tool")
    assert isinstance(resolved, Tool)
    assert resolved.name == "sample_tool"

    wrapped = resolve_tool_path("tests.unit.test_agent:plain_tool_fn")
    assert isinstance(wrapped, Tool)
    assert wrapped.name == "plain_tool_fn"
    assert wrapped.description == "Report the weather for a city."


def test_resolve_tool_path_requires_colon() -> None:
    with pytest.raises(ValueError, match="must look like"):
        resolve_tool_path("tests.unit.test_agent.sample_tool")


def test_resolve_tool_path_non_callable_attr() -> None:
    with pytest.raises(TypeError, match="not a Tool/callable"):
        resolve_tool_path("os:sep")
