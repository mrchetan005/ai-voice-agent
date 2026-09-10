"""Events bridge: livekit AgentSession events -> canonical voiceagent events.

Translator functions are pure and duck-typed (testable with SimpleNamespace);
`attach_bridge` subscribes them and fans canonical events out to user
handlers, the usage collector, and memory. Handler exceptions are logged and
never propagate — an observer must not kill a live session.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
import time
from collections.abc import Callable, Sequence
from typing import Any

from voiceagent.events import (
    AgentReply,
    AgentStateChanged,
    BaseEvent,
    ErrorEvent,
    EventHandler,
    UsageUpdated,
    UserStateChanged,
    UserTranscript,
)
from voiceagent.livekit.compile import SessionRuntime
from voiceagent.memory import Message

logger = logging.getLogger("voiceagent")

_background_tasks: set[asyncio.Task] = set()


def spawn(coro: Any) -> None:
    """Fire-and-forget with a held reference (tasks are GC'd otherwise)."""
    task = asyncio.ensure_future(coro)
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)


def make_dispatcher(
    ids_handlers: Sequence[EventHandler],
) -> Callable[[BaseEvent], None]:
    """Fan one canonical event out to handlers; async handlers get a task."""

    def emit(event: BaseEvent) -> None:
        for handler in ids_handlers:
            try:
                result = handler(event)
                if inspect.isawaitable(result):
                    spawn(result)
            except Exception:
                logger.exception("event handler failed for %s", type(event).__name__)

    return emit


# --- pure translators (unit-testable with SimpleNamespace stubs) -------------


def normalize_model_usage(model_usage: Sequence[Any]) -> dict[str, dict[str, float]]:
    """livekit AgentSessionUsage.model_usage -> {provider: {unit: amount}}.

    Duck-typed and tolerant: unknown entry types are skipped, realtime models
    (audio-token entries) get a `-realtime` provider suffix so they price
    against the realtime rate rows.
    """
    usage: dict[str, dict[str, float]] = {}

    def bump(provider: str, unit: str, amount: Any) -> None:
        try:
            value = float(amount)
        except (TypeError, ValueError):
            return
        if value:
            usage.setdefault(provider, {})[unit] = usage.get(provider, {}).get(unit, 0.0) + value

    for entry in model_usage or ():
        kind = getattr(entry, "type", "")
        provider = str(getattr(entry, "provider", "") or "unknown")
        if kind == "llm_usage":
            audio_in = getattr(entry, "input_audio_tokens", 0)
            audio_out = getattr(entry, "output_audio_tokens", 0)
            if audio_in or audio_out:
                realtime = f"{provider}-realtime"
                bump(realtime, "input_tokens_audio", audio_in)
                bump(realtime, "output_tokens_audio", audio_out)
                bump(realtime, "input_tokens_text", getattr(entry, "input_text_tokens", 0))
                bump(realtime, "output_tokens_text", getattr(entry, "output_text_tokens", 0))
            else:
                bump(provider, "input_tokens", getattr(entry, "input_tokens", 0))
                bump(provider, "output_tokens", getattr(entry, "output_tokens", 0))
        elif kind == "tts_usage":
            bump(provider, "characters", getattr(entry, "characters_count", 0))
        elif kind == "stt_usage":
            bump(provider, "audio_seconds", getattr(entry, "audio_duration", 0.0))
        # interruption/eot usage entries are free — skipped on purpose
    return usage


def translate_transcribed(ids: Any, ev: Any) -> UserTranscript:
    return UserTranscript(ids=ids, text=ev.transcript, final=bool(ev.is_final))


def translate_agent_state(ids: Any, ev: Any) -> AgentStateChanged:
    return AgentStateChanged(ids=ids, state=str(ev.new_state))


def translate_user_state(ids: Any, ev: Any) -> UserStateChanged:
    return UserStateChanged(ids=ids, state=str(ev.new_state))


def translate_conversation_item(ids: Any, ev: Any) -> AgentReply | None:
    item = ev.item
    if getattr(item, "type", "") != "message" or getattr(item, "role", "") != "assistant":
        return None
    return AgentReply(
        ids=ids,
        text=item.text_content or "",
        interrupted=bool(getattr(item, "interrupted", False)),
    )


def attach_bridge(session: Any, runtime: SessionRuntime) -> None:
    """Subscribe canonical translators on a livekit AgentSession."""
    ids = runtime.ids

    def on_transcribed(ev: Any) -> None:
        if ev.is_final:
            runtime.emit(translate_transcribed(ids, ev))

    def on_conversation_item(ev: Any) -> None:
        item = ev.item
        if getattr(item, "type", "") == "message":
            role = getattr(item, "role", "")
            if role in ("user", "assistant") and runtime.memory and runtime.memory_key:
                message = Message(role=role, content=item.text_content or "", ts=time.time())
                spawn(runtime.memory.append(runtime.memory_key, message))
        reply = translate_conversation_item(ids, ev)
        if reply is not None:
            runtime.emit(reply)

    def on_agent_state(ev: Any) -> None:
        runtime.emit(translate_agent_state(ids, ev))

    def on_user_state(ev: Any) -> None:
        runtime.emit(translate_user_state(ids, ev))

    def on_usage(ev: Any) -> None:
        normalized = normalize_model_usage(getattr(ev.usage, "model_usage", ()))
        if normalized:
            runtime.usage.replace(normalized)
            runtime.emit(UsageUpdated(ids=ids, usage=runtime.usage.usage))

    def on_error(ev: Any) -> None:
        runtime.emit(ErrorEvent(ids=ids, message=str(getattr(ev, "error", ev)), recoverable=True))

    session.on("user_input_transcribed", on_transcribed)
    session.on("conversation_item_added", on_conversation_item)
    session.on("agent_state_changed", on_agent_state)
    session.on("user_state_changed", on_user_state)
    session.on("session_usage_updated", on_usage)
    session.on("error", on_error)
