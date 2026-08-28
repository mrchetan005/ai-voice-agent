"""Quickstart: your whole voice agent in ~12 lines.

Offline demo (no API keys):   uv run examples/quickstart.py --mock
Real run (needs DEEPGRAM/GROQ/CARTESIA keys):
                              uv run examples/quickstart.py
Then connect any WS client to ws://localhost:8765 (binary PCM16 @16 kHz in,
24 kHz out; optional ?language=hi-IN&tone=warm per connection).
"""

import asyncio
import sys

from voiceagent import VoiceAgent, run_mock_demo


async def my_agent(ctx):
    await ctx.status("looking_that_up")          # spoken live commentary
    await asyncio.sleep(0.2)                     # pretend work
    if "delete" in ctx.text.lower():
        if not await ctx.approve("That will delete data. Shall I proceed?"):
            return "Okay, cancelled."
        return "Done — the files are deleted."
    return f"You said: {ctx.text}"


if __name__ == "__main__":
    if "--mock" in sys.argv:
        asyncio.run(run_mock_demo(my_agent))
    else:
        VoiceAgent(
            provider="deepgram+groq+cartesia",
            agent=my_agent,
            language="en-US",
            tone="warm",
        ).run("ws://0.0.0.0:8765")
