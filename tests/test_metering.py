"""OFFLINE checks for usage capture: Gemini Live usageMetadata accumulation,
the Groq SSE usage chunk (empty choices!), UsageMeter, derive_outcome.

Run:  uv run tests/test_metering.py
"""

from __future__ import annotations

import asyncio
import sys

import httpx

from appointment_booker.metering import UsageMeter, derive_outcome
from voiceagent.models import SessionConfig
from voiceagent.providers import GeminiLiveProxy, OpenAICompatLLM


class DummyTransport:
    async def recv_frames(self):
        return
        yield

    async def send_frame(self, frame):
        pass

    async def interrupt(self):
        pass

    async def close(self):
        pass


async def main() -> int:
    failures: list[str] = []

    def check(name: str, ok: bool) -> None:
        print(f"  [{'PASS' if ok else 'FAIL'}] {name}")
        if not ok:
            failures.append(name)

    # -- GeminiLiveProxy usageMetadata --------------------------------------
    config = SessionConfig(system_prompt="x")
    proxy = GeminiLiveProxy(config, DummyTransport())

    proxy._accumulate_usage({
        "promptTokenCount": 100, "responseTokenCount": 40, "totalTokenCount": 140,
        "promptTokensDetails": [
            {"modality": "AUDIO", "tokenCount": 90},
            {"modality": "TEXT", "tokenCount": 10},
        ],
        "responseTokensDetails": [{"modality": "AUDIO", "tokenCount": 40}],
    })
    proxy._accumulate_usage({"totalTokenCount": 60, "promptTokenCount": 60})
    check("token sums accumulate across messages",
          proxy.usage["total_tokens"] == 200 and proxy.usage["prompt_tokens"] == 160)
    check("modality breakdown captured",
          proxy.usage["prompt_tokens_audio"] == 90
          and proxy.usage["prompt_tokens_text"] == 10
          and proxy.usage["response_tokens_audio"] == 40)

    proxy._accumulate_usage({"promptTokensDetails": "garbage-shape"})
    check("malformed usageMetadata never raises", True)

    # usageMetadata riding alongside serverContent (non-exclusive check):
    # _handle_message with BOTH keys must count usage even though the
    # interrupted branch returns early. interrupt path needs a bridge=None.
    before = proxy.usage["total_tokens"]
    await proxy._handle_message({
        "usageMetadata": {"totalTokenCount": 5},
        "serverContent": {"interrupted": True},
    })
    check("usage counted on interrupted message (sibling of serverContent)",
          proxy.usage["total_tokens"] == before + 5)

    # -- Groq SSE usage chunk (choices == []) ----------------------------------
    sse_body = "\n".join([
        'data: {"choices": [{"delta": {"content": "Hi"}}]}',
        'data: {"choices": [{"delta": {"content": " there"}}]}',
        'data: {"choices": [], "usage": {"prompt_tokens": 12, "completion_tokens": 5}}',
        "data: [DONE]",
        "",
    ])

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=sse_body,
                              headers={"content-type": "text/event-stream"})

    llm = OpenAICompatLLM({"llm_api_key_env": "FAKE_GROQ_KEY"})
    await llm._client.aclose()
    llm._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    import os
    os.environ["FAKE_GROQ_KEY"] = "test"
    tokens = [t async for t in llm.stream([{"role": "user", "content": "hi"}])]
    check("SSE content still streams", tokens == ["Hi", " there"])
    check("usage chunk with empty choices parsed (no IndexError)",
          llm.usage == {"input_tokens": 12, "output_tokens": 5})
    await llm.aclose()

    # -- UsageMeter ------------------------------------------------------------
    meter = UsageMeter("s1", "919", "voice-inbound", "single")
    meter.add("gemini_flash", "input_tokens", 100)
    meter.add("gemini_flash", "input_tokens", 50)
    meter.add("gemini_flash", "input_tokens", None)      # ignored
    meter.add("gemini_flash", "input_tokens", "junk")    # ignored
    meter.merge_gemini_live({"total_tokens": 200.0, "audio_in_seconds": 12.5})
    meter.merge_split_stack({"asr_audio_seconds": 30.0, "llm_input_tokens": 7,
                             "tts_characters": 400.0})
    snap = meter.snapshot()
    check("meter sums + ignores garbage",
          snap["gemini_flash"]["input_tokens"] == 150)
    check("gemini live merge", snap["gemini_live"]["total_tokens"] == 200)
    check("split-stack merge maps to billing providers",
          snap["deepgram"]["audio_seconds"] == 30
          and snap["groq"]["input_tokens"] == 7
          and snap["cartesia"]["characters"] == 400)

    # -- derive_outcome -----------------------------------------------------------
    check("outcome precedence",
          derive_outcome([{"action": "booked"}, {"action": "cancelled"}], []) == "booked"
          and derive_outcome([{"action": "rescheduled"}], []) == "rescheduled"
          and derive_outcome([], [{"op": "book", "error": "boom"}]) == "error"
          and derive_outcome([], [{"op": "request_email", "status": "NO_REPLY"}]) == "no_action"
          and derive_outcome([], []) == "no_action")

    print(f"\n{'ALL PASS' if not failures else f'{len(failures)} FAILURE(S): {failures}'}")
    return 0 if not failures else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
