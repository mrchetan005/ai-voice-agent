"""Split-stack LLM: OpenAI-compatible streaming chat (Groq default) + token chunking for TTS."""

from __future__ import annotations

import json
import logging
import re
import time
from collections.abc import AsyncIterator, Callable
from typing import Any

import httpx

from ._common import _require_env

logger = logging.getLogger("voiceagent")



# ---------------------------------------------------------------------------
# Token chunking (split-stack TTFB optimization)
# ---------------------------------------------------------------------------

_CLAUSE_PUNCT = re.compile(r"[.,;:!?।]")


async def chunk_tokens(
    tokens: AsyncIterator[str],
    first_words: int = 4,
    max_words: int = 12,
) -> AsyncIterator[str]:
    """Regroup an LLM token stream into TTS-friendly text pieces.

    The FIRST piece flushes as soon as ~``first_words`` words (or any
    punctuation) arrive — that is the single biggest TTFB lever in a split
    stack: TTS starts synthesizing while the LLM is still on token 6.
    Subsequent pieces flush on clause punctuation or ``max_words``, which
    keeps prosody natural (TTS engines phrase better on clause boundaries).
    """
    buffer: list[str] = []
    first_flushed = False
    async for token in tokens:
        if not token:
            continue
        buffer.append(token)
        joined = "".join(buffer)
        words = len(joined.split())
        threshold = first_words if not first_flushed else max_words
        if words >= threshold or (_CLAUSE_PUNCT.search(joined) and words >= 2):
            yield joined
            buffer.clear()
            first_flushed = True
    if buffer:
        yield "".join(buffer)



# ---------------------------------------------------------------------------
# Split stack: OpenAI-compatible LLM (Groq default)
# ---------------------------------------------------------------------------


class OpenAICompatLLM:
    """Streaming chat-completions client for any OpenAI-compatible endpoint."""

    def __init__(self, options: dict[str, Any]) -> None:
        self.base_url: str = options.get("llm_base_url", "https://api.groq.com/openai/v1")
        self.model: str = options.get("llm_model", "openai/gpt-oss-20b")
        self._key_env: str = options.get("llm_api_key_env", "GROQ_API_KEY")
        self._extra: dict[str, Any] = dict(options.get("llm_extra", {}))
        # gpt-oss reasoning models: force low effort for voice TTFB.
        if "gpt-oss" in self.model and "reasoning_effort" not in self._extra:
            self._extra["reasoning_effort"] = "low"
        self._client = httpx.AsyncClient(timeout=httpx.Timeout(60.0, connect=10.0))
        self.usage: dict[str, float] = {"input_tokens": 0.0, "output_tokens": 0.0}

    async def stream(
        self,
        messages: list[dict[str, str]],
        on_first_token: Callable[[float], None] | None = None,
    ) -> AsyncIterator[str]:
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "stream": True,
            # Ask for the final usage chunk (Groq also mirrors it under
            # x_groq); it arrives with choices == [] — guard the index.
            "stream_options": {"include_usage": True},
            **self._extra,
        }
        started = time.monotonic()
        first = True
        async with self._client.stream(
            "POST",
            f"{self.base_url}/chat/completions",
            json=payload,
            headers={"Authorization": f"Bearer {_require_env(self._key_env)}"},
        ) as response:
            response.raise_for_status()
            async for line in response.aiter_lines():
                if not line.startswith("data: "):
                    continue
                data = line[6:]
                if data == "[DONE]":
                    return
                obj = json.loads(data)
                if usage := (obj.get("usage") or (obj.get("x_groq") or {}).get("usage")):
                    self.usage["input_tokens"] += usage.get("prompt_tokens", 0) or 0
                    self.usage["output_tokens"] += usage.get("completion_tokens", 0) or 0
                choices = obj.get("choices") or []
                delta = choices[0].get("delta", {}).get("content") if choices else None
                if delta:
                    if first and on_first_token is not None:
                        on_first_token((time.monotonic() - started) * 1000.0)
                        first = False
                    yield delta

    async def aclose(self) -> None:
        await self._client.aclose()


def make_llm_agent(
    llm: OpenAICompatLLM,
    on_first_token: Callable[[float], None] | None = None,
) -> Callable[[Any], AsyncIterator[str]]:
    """Default agent when the user plugs none: a plain streaming LLM chat.

    The returned handler follows the normal AgentContext contract, so
    swapping it for a real orchestrator later is one constructor argument.
    """

    def handler(ctx: Any) -> AsyncIterator[str]:
        messages = [{"role": "system", "content": (
            f"{ctx.config.system_prompt} Respond in {ctx.config.language} "
            f"with a {ctx.config.tone} tone. Keep answers short: this is a "
            f"voice conversation."
        )}]
        messages.extend(ctx.history[-12:])  # trailing window; voice = short turns
        return llm.stream(messages, on_first_token=on_first_token)

    return handler

