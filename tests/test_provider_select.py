"""OFFLINE checks for provider management: engine selection, brain-mode
matrix, split-stack env knobs, voice aliases, swappable brain LLM, and
OpenAI Realtime usage accounting.

Run:  uv run tests/test_provider_select.py
"""

from __future__ import annotations

import os
import sys

from voiceagent.models import SessionConfig
from voiceagent.providers import (
    GeminiLiveProxy,
    OpenAIRealtimeProxy,
    SplitStackProxy,
)
from whatsapp_agent.cli import (
    _PROVIDER_RATES,
    _build_proxy,
    _resolve_brain,
    _resolve_voice,
    _split_provider_options,
)
from whatsapp_agent.infra.metering import UsageMeter

ROHAN = "4877b818-c7fe-4c89-b1cf-eadf8e23da72"


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


def main() -> int:
    failures: list[str] = []

    def check(name: str, ok: bool) -> None:
        print(f"  [{'PASS' if ok else 'FAIL'}] {name}")
        if not ok:
            failures.append(name)

    # -- engine selection + rates -------------------------------------------
    config = SessionConfig(system_prompt="x", provider_options={
        "asr": "deepgram", "tts": "cartesia"})
    check("proxy classes per provider",
          isinstance(_build_proxy("gemini-live", config, DummyTransport()), GeminiLiveProxy)
          and isinstance(_build_proxy("openai-realtime", config, DummyTransport()), OpenAIRealtimeProxy)
          and isinstance(_build_proxy("split", config, DummyTransport()), SplitStackProxy))
    check("OpenAI Realtime forces 24k both ways",
          _PROVIDER_RATES["openai-realtime"] == (24_000, 24_000)
          and _PROVIDER_RATES["gemini-live"] == (16_000, 24_000))

    # -- brain matrix -----------------------------------------------------------
    check("brain matrix: gemini keeps single, others forced dual",
          _resolve_brain("gemini-live", "single") == "single"
          and _resolve_brain("gemini-live", "dual") == "dual"
          and _resolve_brain("split", "single") == "dual"
          and _resolve_brain("openai-realtime", "single") == "dual")

    # -- split env knobs -----------------------------------------------------------
    for key in ("SPLIT_ASR", "SPLIT_ASR_LANGUAGE", "SPLIT_ASR_MODEL", "SPLIT_TTS"):
        os.environ.pop(key, None)
    opts = _split_provider_options()
    check("split defaults: deepgram + cartesia + MULTILINGUAL",
          opts == {"asr": "deepgram", "tts": "cartesia", "asr_language": "multi"})
    os.environ["SPLIT_ASR"] = "deepgram-flux"
    os.environ["SPLIT_ASR_MODEL"] = "flux-general-multi"
    try:
        opts = _split_provider_options()
        check("split env overrides applied",
              opts["asr"] == "deepgram-flux" and opts["asr_model"] == "flux-general-multi")
    finally:
        del os.environ["SPLIT_ASR"], os.environ["SPLIT_ASR_MODEL"]

    # -- voice aliases ---------------------------------------------------------------
    os.environ.pop("VOICEAGENT_VOICE_ID", None)
    check("voice aliases + passthrough",
          _resolve_voice("rohan") == ROHAN
          and _resolve_voice("Kavita") == "56e35e2d-6eb6-4226-ab8b-9776515a7094"
          and _resolve_voice("Despina") == "Despina"
          and _resolve_voice(None) is None)
    os.environ["VOICEAGENT_VOICE_ID"] = "rohan"
    try:
        check("env voice falls back and resolves aliases",
              _resolve_voice(None) == ROHAN)
    finally:
        del os.environ["VOICEAGENT_VOICE_ID"]

    # -- brain LLM factory -----------------------------------------------------------
    from whatsapp_agent.agent.brain import make_brain_llm

    os.environ.setdefault("GOOGLE_API_KEY", "fake-for-offline-test")
    os.environ["GROQ_API_KEY"] = os.environ.get("GROQ_API_KEY") or "fake"
    os.environ["OPENROUTER_API_KEY"] = "fake-or"
    try:
        llm, label = make_brain_llm("gemini-3.6-flash")
        check("bare model -> gemini brain", label == "gemini_flash")

        llm, label = make_brain_llm("groq/openai/gpt-oss-20b")
        check("groq spec -> ChatOpenAI on groq base, model kept intact",
              label == "groq"
              and "groq.com" in str(getattr(llm, "openai_api_base", ""))
              and llm.model_name == "openai/gpt-oss-20b")

        llm, label = make_brain_llm("openrouter/anthropic/claude-sonnet-5")
        check("openrouter spec (BYOK gateway) supported",
              label == "openrouter"
              and "openrouter.ai" in str(getattr(llm, "openai_api_base", "")))

        os.environ.pop("SCHEDULER_BASE_URL", None)
        try:
            make_brain_llm("custom/my-litellm-alias")
            check("custom without SCHEDULER_BASE_URL raises", False)
        except RuntimeError:
            check("custom without SCHEDULER_BASE_URL raises", True)

        os.environ["SCHEDULER_BASE_URL"] = "http://localhost:4000/v1"
        os.environ["CUSTOM_LLM_API_KEY"] = "sk-litellm"
        llm, label = make_brain_llm("custom/my-litellm-alias")
        check("custom spec -> LiteLLM-style gateway",
              label == "custom_llm"
              and "localhost:4000" in str(getattr(llm, "openai_api_base", "")))

        os.environ.pop("OPENROUTER_API_KEY")
        try:
            make_brain_llm("openrouter/some/model")
            check("missing gateway key raises with the env var named", False)
        except RuntimeError as exc:
            check("missing gateway key raises with the env var named",
                  "OPENROUTER_API_KEY" in str(exc))
    finally:
        for key in ("SCHEDULER_BASE_URL", "CUSTOM_LLM_API_KEY"):
            os.environ.pop(key, None)

    # -- OpenAI Realtime usage accounting ------------------------------------------
    proxy = OpenAIRealtimeProxy(SessionConfig(system_prompt="x"), DummyTransport())
    proxy._accumulate_usage({
        "input_tokens": 900, "output_tokens": 250,
        "input_token_details": {"text_tokens": 300, "audio_tokens": 600},
        "output_token_details": {"text_tokens": 50, "audio_tokens": 200},
    })
    proxy._accumulate_usage({"input_tokens": 100})
    check("realtime usage sums with text/audio split",
          proxy.usage["input_tokens"] == 1000
          and proxy.usage["input_tokens_audio"] == 600
          and proxy.usage["output_tokens_audio"] == 200
          and proxy.usage["turns"] == 2)
    proxy._accumulate_usage({"input_token_details": "garbage"})
    check("malformed realtime usage never raises", True)

    meter = UsageMeter("s1", "919", "voice-outbound", "dual")
    meter.merge_openai_realtime(proxy.usage)
    check("meter maps realtime usage to openai_realtime provider",
          meter.snapshot()["openai_realtime"]["input_tokens_audio"] == 600)

    print(f"\n{'ALL PASS' if not failures else f'{len(failures)} FAILURE(S): {failures}'}")
    return 0 if not failures else 1


if __name__ == "__main__":
    sys.exit(main())
