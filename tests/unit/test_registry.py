"""Unit tests for the provider registry (offline, no provider SDKs)."""

from __future__ import annotations

import uuid

import pytest

from voiceagent.registry import (
    ProviderRegistry,
    ProviderSpec,
    UnknownProviderError,
    register_llm,
    registry,
)


def test_create_passes_spec_through_to_factory() -> None:
    reg = ProviderRegistry()
    seen: list[ProviderSpec] = []

    def factory(spec: ProviderSpec) -> str:
        seen.append(spec)
        return "built"

    reg.register("llm", "echo", factory)
    spec = ProviderSpec(model="gpt-x", language="hi", voice="a", temperature=0.2)

    assert reg.create("llm", "echo", spec) == "built"
    assert seen == [spec]
    assert seen[0] is spec


def test_create_without_spec_builds_default_spec() -> None:
    reg = ProviderRegistry()
    reg.register("tts", "echo", lambda spec: spec)

    spec = reg.create("tts", "echo")

    assert isinstance(spec, ProviderSpec)
    assert spec == ProviderSpec()
    assert spec.model is None
    assert spec.language == "en"
    assert spec.options == {}


def test_duplicate_register_raises_unless_overwrite() -> None:
    reg = ProviderRegistry()
    reg.register("stt", "dup", lambda spec: "first")

    with pytest.raises(ValueError, match="already registered"):
        reg.register("stt", "dup", lambda spec: "second")
    assert reg.create("stt", "dup") == "first"

    reg.register("stt", "dup", lambda spec: "second", overwrite=True)
    assert reg.create("stt", "dup") == "second"


def test_same_name_across_kinds_is_not_a_duplicate() -> None:
    reg = ProviderRegistry()
    reg.register("llm", "acme", lambda spec: "llm")
    reg.register("tts", "acme", lambda spec: "tts")

    assert reg.create("llm", "acme") == "llm"
    assert reg.create("tts", "acme") == "tts"


def test_unknown_provider_error_lists_known_and_requested() -> None:
    reg = ProviderRegistry()
    reg.register("llm", "alpha", lambda spec: None)
    reg.register("llm", "beta", lambda spec: None)

    with pytest.raises(UnknownProviderError) as excinfo:
        reg.create("llm", "missing")

    err = excinfo.value
    assert err.kind == "llm"
    assert err.name == "missing"
    message = str(err)
    assert "missing" in message
    assert "alpha" in message
    assert "beta" in message


def test_unknown_provider_error_on_empty_registry() -> None:
    reg = ProviderRegistry()

    with pytest.raises(UnknownProviderError) as excinfo:
        reg.create("vad", "nope")

    assert "(none)" in str(excinfo.value)


def test_register_invalid_kind_raises_value_error() -> None:
    reg = ProviderRegistry()

    with pytest.raises(ValueError, match="unknown provider kind"):
        reg.register("nonsense", "x", lambda spec: None)  # type: ignore[arg-type]


def test_names_are_sorted_and_kind_scoped() -> None:
    reg = ProviderRegistry()
    reg.register("llm", "zulu", lambda spec: None)
    reg.register("llm", "alpha", lambda spec: None)
    reg.register("stt", "midway", lambda spec: None)

    assert reg.names("llm") == ["alpha", "zulu"]
    assert reg.names("stt") == ["midway"]
    assert reg.names("tts") == []


def test_contains_checks_kind_name_pairs() -> None:
    reg = ProviderRegistry()
    reg.register("realtime", "wire", lambda spec: None)

    assert ("realtime", "wire") in reg
    assert ("llm", "wire") not in reg
    assert ("realtime", "other") not in reg


def test_register_llm_decorator_uses_module_singleton() -> None:
    name = f"test-unique-{uuid.uuid4().hex}"

    def factory(spec: ProviderSpec) -> str:
        return f"llm:{spec.language}"

    decorated = register_llm(name, overwrite=True)(factory)

    assert decorated is factory
    assert ("llm", name) in registry
    assert registry.create("llm", name) == "llm:en"
