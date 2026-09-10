"""Unit tests for voiceagent.prompts (offline, no markers)."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from voiceagent.prompts import PromptConfig, PromptError, PromptTemplate

# --- variables parsing ---


def test_variables_simple() -> None:
    assert PromptTemplate("hi {a} and {b}").variables == frozenset({"a", "b"})


def test_variables_dotted_reports_root() -> None:
    assert PromptTemplate("hi {user.name}").variables == frozenset({"user"})


def test_variables_indexed_reports_root() -> None:
    assert PromptTemplate("first: {items[0]}").variables == frozenset({"items"})


def test_variables_literal_braces_ignored() -> None:
    tpl = PromptTemplate("json: {{\"key\": 1}} uses {a}")
    assert tpl.variables == frozenset({"a"})


def test_variables_empty_for_plain_text() -> None:
    assert PromptTemplate("no placeholders here").variables == frozenset()


def test_invalid_template_raises_at_construction() -> None:
    with pytest.raises(PromptError):
        PromptTemplate("unbalanced {a")
    with pytest.raises(PromptError):
        PromptTemplate("unbalanced } brace")


# --- render ---


def test_render_uses_defaults() -> None:
    tpl = PromptTemplate("hello {name}", defaults={"name": "world"})
    assert tpl.render() == "hello world"


def test_render_override_beats_default() -> None:
    tpl = PromptTemplate("hello {name}", defaults={"name": "world"})
    assert tpl.render(name="ada") == "hello ada"


def test_render_missing_variables_raise_listing_names() -> None:
    tpl = PromptTemplate("{b} {a} {c}", defaults={"c": "x"})
    with pytest.raises(PromptError, match="missing prompt variables: a, b"):
        tpl.render()


def test_render_extra_variables_ignored() -> None:
    tpl = PromptTemplate("hello {name}")
    assert tpl.render(name="ada", unused="ignored") == "hello ada"


def test_render_no_variable_template_returns_text() -> None:
    assert PromptTemplate("static text").render() == "static text"


def test_render_dotted_and_indexed_access() -> None:
    tpl = PromptTemplate("{user.name} likes {items[0]}")
    out = tpl.render(user=SimpleNamespace(name="ada"), items=["tea"])
    assert out == "ada likes tea"


def test_render_unescapes_literal_braces_alongside_variable() -> None:
    tpl = PromptTemplate("{{\"k\": 1}} and {a}")
    assert tpl.render(a="x") == "{\"k\": 1} and x"


# --- composition ---


def test_add_template_joins_with_blank_line() -> None:
    combined = PromptTemplate("first {a}") + PromptTemplate("second {b}")
    assert combined.template == "first {a}\n\nsecond {b}"
    assert combined.variables == frozenset({"a", "b"})
    assert combined.render(a="1", b="2") == "first 1\n\nsecond 2"


def test_add_string_coerced_to_template() -> None:
    combined = PromptTemplate("intro") + "outro {x}"
    assert isinstance(combined, PromptTemplate)
    assert combined.render(x="!") == "intro\n\noutro !"


def test_add_defaults_merge_right_wins() -> None:
    left = PromptTemplate("{a} {b}", defaults={"a": "L", "b": "L"})
    right = PromptTemplate("{c}", defaults={"b": "R", "c": "R"})
    combined = left + right
    assert combined.defaults == {"a": "L", "b": "R", "c": "R"}


# --- from_file ---


def test_from_file_reads_template(tmp_path: Path) -> None:
    path = tmp_path / "greet.txt"
    path.write_text("hola {name} — bienvenida", encoding="utf-8")
    tpl = PromptTemplate.from_file(path, defaults={"name": "ada"})
    assert tpl.variables == frozenset({"name"})
    assert tpl.render() == "hola ada — bienvenida"


# --- PromptConfig ---


def test_config_requires_a_source() -> None:
    with pytest.raises(ValidationError):
        PromptConfig()


def test_config_rejects_both_sources() -> None:
    with pytest.raises(ValidationError):
        PromptConfig(text="hi", file="prompt.txt")


def test_config_resolve_text_with_variables_as_defaults() -> None:
    cfg = PromptConfig(text="hello {name}", variables={"name": "world"})
    tpl = cfg.resolve()
    assert isinstance(tpl, PromptTemplate)
    assert tpl.render() == "hello world"


def test_config_resolve_file(tmp_path: Path) -> None:
    path = tmp_path / "p.txt"
    path.write_text("count: {n}", encoding="utf-8")
    cfg = PromptConfig(file=str(path), variables={"n": "3"})
    assert cfg.resolve().render() == "count: 3"
