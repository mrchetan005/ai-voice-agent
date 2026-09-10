"""Realtime example: the SAME agent shape, one model does STT+LLM+TTS.

Needs GOOGLE_API_KEY (Gemini Live):
    uv run --env-file .env python examples/agents/sales_realtime.py console
"""

from __future__ import annotations

from examples.tools_demo import book_appointment, get_available_slots
from voiceagent import VoiceAgent, run

agent = VoiceAgent(
    name="sales-realtime",
    mode="realtime",
    llm="google",  # realtime provider; model default = plugin default
    system_prompt=(
        "You are a concise, friendly sales assistant for {company}. "
        "Answer in at most two short sentences — this is a voice call."
    ),
    tools=[get_available_slots, book_appointment],
    greeting="Greet the caller briefly and ask how you can help.",
)

agent.config.prompt.variables.setdefault("company", "Acme Corp")

if __name__ == "__main__":
    run(agent)
