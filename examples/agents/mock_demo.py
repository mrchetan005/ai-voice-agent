"""Zero-credit demo agent: mock STT/LLM/TTS prove the full media path.

Run without any provider keys:
    uv run python examples/agents/mock_demo.py console      # local mic/speaker
    uv run python examples/agents/mock_demo.py dev          # against LiveKit
"""

from __future__ import annotations

from voiceagent import VoiceAgent, run

agent = VoiceAgent(
    name="mock-demo",
    llm="mock",
    stt="mock",
    tts="mock",
    system_prompt="You are a demo agent. Echo what the user says.",
)

if __name__ == "__main__":
    run(agent)
