"""Pipeline example: Deepgram STT -> Gemini -> Cartesia TTS, with tools.

Needs DEEPGRAM_API_KEY, GOOGLE_API_KEY, CARTESIA_API_KEY:
    uv run --env-file .env python examples/agents/sales_pipeline.py console
"""

from __future__ import annotations

from examples.tools_demo import book_appointment, current_datetime, get_available_slots
from voiceagent import BaseEvent, VoiceAgent, run

agent = VoiceAgent(
    name="sales-pipeline",
    llm="google",
    model="gemini-2.5-flash",
    stt="deepgram:nova-3",
    tts="cartesia",
    system_prompt=(
        "You are a concise, friendly sales assistant for {company}. "
        "Answer in at most two short sentences — this is a voice call."
    ),
    tools=[get_available_slots, book_appointment, current_datetime],
    language="en",
    temperature=0.4,
    greeting="Greet the caller briefly and ask how you can help.",
    turn_detection="multilingual",
)


@agent.on_event
def log_event(event: BaseEvent) -> None:
    print(f"[{type(event).__name__}] {event}")


# Prompt variables come from dispatch metadata at session time; give a default
# for console mode:
agent.config.prompt.variables.setdefault("company", "Acme Corp")

if __name__ == "__main__":
    run(agent)
