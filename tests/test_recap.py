"""OFFLINE checks for the post-call recap sender (fake LLM, fake WhatsApp).

Run:  uv run tests/test_recap.py

Asserts the turn threshold (no recap for wrong-number calls), single-send
idempotency, and that an LLM failure is swallowed instead of breaking
teardown.
"""

from __future__ import annotations

import asyncio
import sys

from whatsapp_agent.agent.recap import RecapSender


class FakeWA:
    def __init__(self) -> None:
        self.texts: list[tuple[str, str]] = []

    async def send_text(self, to, body):
        self.texts.append((to, body))


class FakeLLM:
    def __init__(self, reply: str = "*Call recap*\n- discussed a demo") -> None:
        self.reply = reply
        self.calls: list = []

    async def ainvoke(self, messages):
        self.calls.append(messages)

        class _Msg:
            content = self.reply

        return _Msg()


class ExplodingLLM:
    async def ainvoke(self, messages):
        raise RuntimeError("model down")


TURNS = [
    ("user", "hi, I want an appointment"),
    ("assistant", "sure, when suits you?"),
    ("user", "tomorrow at 3"),
    ("assistant", "booked!"),
]


async def main() -> int:
    failures: list[str] = []

    def check(name: str, ok: bool) -> None:
        print(f"  [{'PASS' if ok else 'FAIL'}] {name}")
        if not ok:
            failures.append(name)

    wa = FakeWA()
    recap = RecapSender(wa, "91999", "Acme", model_name="fake",
                        min_user_turns=2, llm=FakeLLM())
    check("trivial call (1 user turn) skipped",
          not await recap.send([("user", "wrong number"), ("assistant", "oh, sorry!")], [])
          and not wa.texts)

    check("meaningful call sends the recap",
          await recap.send(TURNS, [{"action": "booked", "uid": "u1"}])
          and len(wa.texts) == 1 and "recap" in wa.texts[0][1].lower())

    check("second send is a no-op (idempotent)",
          not await recap.send(TURNS, []) and len(wa.texts) == 1)

    wa2 = FakeWA()
    llm = FakeLLM()
    recap2 = RecapSender(wa2, "91999", "Acme", model_name="fake",
                         min_user_turns=2, llm=llm)
    await recap2.send(TURNS, [{"action": "booked", "uid": "u1"}])
    prompt_blob = str(llm.calls[0])
    check("ground-truth facts reach the LLM, not just the transcript",
          "u1" in prompt_blob and "booked" in prompt_blob)

    wa3 = FakeWA()
    recap3 = RecapSender(wa3, "91999", "Acme", model_name="fake",
                         min_user_turns=2, llm=ExplodingLLM())
    check("LLM failure swallowed (returns False, nothing sent)",
          not await recap3.send(TURNS, []) and not wa3.texts)

    print(f"\n{'ALL PASS' if not failures else f'{len(failures)} FAILURE(S): {failures}'}")
    return 0 if not failures else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
