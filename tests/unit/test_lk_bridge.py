"""Bridge translators and dispatcher — pure, offline, SimpleNamespace stubs."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

pytest.importorskip("livekit.agents")

from voiceagent.events import AgentReply, SessionIDs, UsageUpdated, UserTranscript
from voiceagent.livekit.bridge import (
    attach_bridge,
    make_dispatcher,
    normalize_model_usage,
    translate_agent_state,
    translate_conversation_item,
    translate_transcribed,
)
from voiceagent.livekit.compile import SessionRuntime
from voiceagent.memory.inmemory import InMemoryMemory

IDS = SessionIDs(agent_id="a", session_id="s")


def _llm_usage(**kw):
    base = {
        "type": "llm_usage",
        "provider": "google",
        "input_tokens": 0,
        "output_tokens": 0,
        "input_audio_tokens": 0,
        "output_audio_tokens": 0,
        "input_text_tokens": 0,
        "output_text_tokens": 0,
    }
    base.update(kw)
    return SimpleNamespace(**base)


def test_normalize_plain_llm_usage() -> None:
    usage = normalize_model_usage([_llm_usage(input_tokens=100, output_tokens=40)])
    assert usage == {"google": {"input_tokens": 100.0, "output_tokens": 40.0}}


def test_normalize_realtime_llm_usage_gets_suffix_and_splits() -> None:
    usage = normalize_model_usage(
        [
            _llm_usage(
                input_audio_tokens=500,
                output_audio_tokens=900,
                input_text_tokens=50,
                output_text_tokens=20,
            )
        ]
    )
    assert usage == {
        "google-realtime": {
            "input_tokens_audio": 500.0,
            "output_tokens_audio": 900.0,
            "input_tokens_text": 50.0,
            "output_tokens_text": 20.0,
        }
    }


def test_normalize_tts_stt_and_unknown() -> None:
    usage = normalize_model_usage(
        [
            SimpleNamespace(type="tts_usage", provider="cartesia", characters_count=120),
            SimpleNamespace(type="stt_usage", provider="deepgram", audio_duration=33.5),
            SimpleNamespace(type="eot_usage", provider="livekit", total_requests=4),
            SimpleNamespace(type="mystery_usage", provider="x", things=1),
        ]
    )
    assert usage == {
        "cartesia": {"characters": 120.0},
        "deepgram": {"audio_seconds": 33.5},
    }


def test_translators() -> None:
    t = translate_transcribed(IDS, SimpleNamespace(transcript="hi", is_final=True))
    assert isinstance(t, UserTranscript) and t.text == "hi" and t.final

    s = translate_agent_state(IDS, SimpleNamespace(old_state="listening", new_state="thinking"))
    assert s.state == "thinking"

    item = SimpleNamespace(type="message", role="assistant", text_content="yo", interrupted=True)
    r = translate_conversation_item(IDS, SimpleNamespace(item=item))
    assert isinstance(r, AgentReply) and r.text == "yo" and r.interrupted

    user_item = SimpleNamespace(type="message", role="user", text_content="q", interrupted=False)
    assert translate_conversation_item(IDS, SimpleNamespace(item=user_item)) is None


async def test_dispatcher_isolates_handler_failures() -> None:
    seen: list[str] = []

    def bad(event):
        raise RuntimeError("boom")

    async def good(event):
        seen.append(type(event).__name__)

    emit = make_dispatcher([bad, good])
    emit(UserTranscript(ids=IDS, text="x"))
    await asyncio.sleep(0)
    assert seen == ["UserTranscript"]


class FakeSession:
    """Minimal EventEmitter lookalike."""

    def __init__(self) -> None:
        self.handlers: dict[str, list] = {}

    def on(self, event: str, cb) -> None:
        self.handlers.setdefault(event, []).append(cb)

    def fire(self, event: str, payload) -> None:
        for cb in self.handlers.get(event, []):
            cb(payload)


async def test_attach_bridge_memory_and_usage_flow() -> None:
    events: list = []
    memory = InMemoryMemory()
    runtime = SessionRuntime(ids=IDS, memory=memory, memory_key="user-7")
    runtime.emit = make_dispatcher([events.append])

    session = FakeSession()
    attach_bridge(session, runtime)

    item = SimpleNamespace(type="message", role="user", text_content="hello", interrupted=False)
    session.fire("conversation_item_added", SimpleNamespace(item=item))
    await asyncio.sleep(0)
    assert [m["content"] for m in await memory.load("user-7")] == ["hello"]

    session.fire(
        "session_usage_updated",
        SimpleNamespace(usage=SimpleNamespace(model_usage=[_llm_usage(input_tokens=10)])),
    )
    updated = [e for e in events if isinstance(e, UsageUpdated)]
    assert updated and updated[-1].usage["google"]["input_tokens"] == 10.0

    # cumulative snapshots replace, not double-count
    session.fire(
        "session_usage_updated",
        SimpleNamespace(usage=SimpleNamespace(model_usage=[_llm_usage(input_tokens=25)])),
    )
    assert runtime.usage.usage["google"]["input_tokens"] == 25.0

    # interim transcripts are not emitted
    session.fire("user_input_transcribed", SimpleNamespace(transcript="par", is_final=False))
    assert not [e for e in events if isinstance(e, UserTranscript)]
