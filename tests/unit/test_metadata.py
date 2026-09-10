"""Tests for the session dispatch metadata wire format."""

from __future__ import annotations

from voiceagent.metadata import SessionMetadata


def test_roundtrip_preserves_all_fields() -> None:
    meta = SessionMetadata(
        v=2,
        tenant_id="acme",
        agent_id="agent-7",
        session_id="sess-42",
        channel="sip",
        user_id="user-9",
        call_id="call-123",
        prompt_vars={"name": "Ada", "slot": "3pm"},
        memory_key="mem:acme:user-9",
        record=True,
        language="hi",
    )
    assert SessionMetadata.from_json(meta.to_json()) == meta


def test_from_json_none_returns_defaults() -> None:
    meta = SessionMetadata.from_json(None)
    assert meta == SessionMetadata()
    assert meta.tenant_id == "default"
    assert meta.channel == "browser"
    assert meta.prompt_vars == {}
    assert meta.record is False


def test_from_json_empty_string_returns_defaults() -> None:
    assert SessionMetadata.from_json("") == SessionMetadata()


def test_from_json_garbage_returns_defaults() -> None:
    assert SessionMetadata.from_json("not json {{{") == SessionMetadata()


def test_from_json_array_returns_defaults() -> None:
    assert SessionMetadata.from_json("[1, 2, 3]") == SessionMetadata()


def test_from_json_scalar_returns_defaults() -> None:
    assert SessionMetadata.from_json("42") == SessionMetadata()


def test_unknown_fields_ignored() -> None:
    meta = SessionMetadata.from_json(
        '{"tenant_id": "acme", "future_field": 123, "another": {"x": 1}}'
    )
    assert meta.tenant_id == "acme"
    assert meta.agent_id == ""


def test_prompt_vars_values_coerced_to_str() -> None:
    meta = SessionMetadata.from_json('{"prompt_vars": {"n": 3, "flag": true, "s": "ok"}}')
    assert meta.prompt_vars == {"n": "3", "flag": "True", "s": "ok"}


def test_prompt_vars_non_dict_dropped() -> None:
    meta = SessionMetadata.from_json('{"prompt_vars": [1, 2], "tenant_id": "acme"}')
    assert meta.prompt_vars == {}
    assert meta.tenant_id == "acme"


def test_prompt_vars_null_dropped() -> None:
    assert SessionMetadata.from_json('{"prompt_vars": null}').prompt_vars == {}


def test_wrong_typed_known_field_degrades_to_field_default() -> None:
    meta = SessionMetadata.from_json(
        '{"v": "banana", "record": "yes", "agent_id": 7, "user_id": 12, "session_id": "s1"}'
    )
    assert meta.v == 1
    assert meta.record is False  # "yes" must not be treated as truthy
    assert meta.agent_id == ""
    assert meta.user_id is None
    assert meta.session_id == "s1"  # well-typed fields survive


def test_bool_not_accepted_for_version_int() -> None:
    assert SessionMetadata.from_json('{"v": true}').v == 1
