"""Hallucination + health auditing.

Two layers:
1. `deterministic_flags` — free rule checks run on EVERY session at write
   time, each grounded in a hard fact (API results, store state, math).
2. A sampled LLM judge, run manually via CLI (credit-frugal):
       uv run --env-file .env python -m appointment_booker.audit --days 7 --sample 10
   Flagged sessions are sampled first; verdicts land in
   voiceagent_sessions.audit as strict JSON.
"""

from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import json
import logging
import os
import re
import sys
from typing import Any

from appointment_booker.prompts import HALLUCINATION_JUDGE_PROMPT
from appointment_booker.stores import SessionStore

logger = logging.getLogger("appointment_booker")

# Assistant statements that CLAIM a completed booking. Questions and
# negations are excluded — the flag fires only when a completion claim has
# no matching API success in session_actions (the ground truth).
_CLAIM_RE = re.compile(
    r"(?:appointment|booking|meeting|slot).{0,60}?(?:booked|confirmed|scheduled)"
    r"|(?:booked|confirmed|scheduled).{0,60}?(?:appointment|booking|meeting|slot)"
    r"|बुक (?:हो|कर दि|कर दी)|कन्फर्म हो|तय हो|पक्क",
    re.IGNORECASE | re.DOTALL,
)
_NEGATION_RE = re.compile(
    r"\b(?:not|no|couldn'?t|can'?t|cannot|unable|fail(?:ed)?|won'?t)\b|नहीं",
    re.IGNORECASE,
)
_DEVANAGARI = re.compile(r"[ऀ-ॿ]")


def deterministic_flags(
    turns: list[tuple[str, str]] | list[list[str]],
    actions: list[dict[str, str]],
    errors: list[dict[str, str]],
    *,
    db_degraded: bool,
    duration_s: float,
    language: str = "",
) -> list[str]:
    flags: list[str] = []
    assistant_texts = [text for role, text in turns if role == "assistant"]

    succeeded = {a.get("action") for a in actions} & {"booked", "rescheduled"}
    claimed = any(
        _CLAIM_RE.search(text)
        and not text.strip().endswith("?")
        and not _NEGATION_RE.search(text)
        for text in assistant_texts
    )
    if claimed and not succeeded:
        flags.append("claimed_booking_no_api_success")

    if any(err.get("error") for err in errors):
        flags.append("booking_failed_during_call")
    if any(err.get("status") == "NO_REPLY" for err in errors):
        flags.append("email_request_no_reply")
    if db_degraded:
        flags.append("db_degraded")
    user_turns = sum(1 for role, _ in turns if role == "user")
    if duration_s < 10 or user_turns == 0:
        flags.append("short_session")
    if language.lower().startswith("hi") and assistant_texts and not any(
        _DEVANAGARI.search(text) for text in assistant_texts
    ):
        flags.append("language_mismatch")
    return flags


# ---------------------------------------------------------------------------
# Sampled LLM judge
# ---------------------------------------------------------------------------

_TRANSCRIPT_CHAR_CAP = 6000


def _parse_verdict(text: str) -> dict[str, Any]:
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", cleaned, flags=re.DOTALL)
    try:
        verdict = json.loads(cleaned)
        if not isinstance(verdict, dict):
            raise ValueError("verdict is not an object")
        return verdict
    except Exception:
        return {"error": "unparseable", "raw": text[:500], "score": None}


def _render_transcript(turns: list[list[str]]) -> str:
    lines = [f"{role}: {text}" for role, text in turns]
    transcript = "\n".join(lines)
    while len(transcript) > _TRANSCRIPT_CHAR_CAP and lines:
        lines.pop(0)  # drop oldest turns first
        transcript = "\n".join(lines)
    return transcript


async def run_audit(
    days: int, sample: int, model: str | None = None,
    dry_run: bool = False, llm: Any = None, store: Any = None,
) -> int:
    if store is None:
        store = SessionStore(os.environ["DATABASE_URL"])
        await store.connect()
    try:
        since = dt.datetime.now(dt.UTC) - dt.timedelta(days=days)
        rows = await store.unaudited(since, sample)
        if not rows:
            print("nothing to audit in the window")
            return 0
        if llm is None:
            from langchain_google_genai import ChatGoogleGenerativeAI

            llm = ChatGoogleGenerativeAI(
                model=model or os.environ.get("JUDGE_MODEL", "gemini-3.6-flash"),
                temperature=0,
            )
        audited = 0
        for row in rows:
            prompt = HALLUCINATION_JUDGE_PROMPT.format(
                channel=row.get("channel", ""),
                actions=json.dumps(row.get("actions") or [], default=str),
                transcript=_render_transcript(row.get("turns") or []),
            )
            reply = await llm.ainvoke([("user", prompt)])
            content = reply.content if isinstance(reply.content, str) else str(reply.content)
            verdict = _parse_verdict(content)
            usage = getattr(reply, "usage_metadata", None) or {}
            verdict["judge"] = {
                "model": getattr(llm, "model", str(model or "")),
                "input_tokens": usage.get("input_tokens", 0),
                "output_tokens": usage.get("output_tokens", 0),
                "judged_at": dt.datetime.now(dt.UTC).isoformat(),
            }
            print(f"  {row['session_id']}: score={verdict.get('score')} "
                  f"summary={verdict.get('summary', verdict.get('error', ''))!r}")
            if not dry_run:
                await store.save_audit(row["session_id"], verdict)
            audited += 1
        print(f"audited {audited} session(s){' (dry run — nothing saved)' if dry_run else ''}")
        return 0
    finally:
        await store.close()


def main() -> int:
    parser = argparse.ArgumentParser(description="sampled hallucination audit")
    parser.add_argument("--days", type=int, default=7)
    parser.add_argument("--sample", type=int, default=10)
    parser.add_argument("--model", default=None)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO)
    if sys.platform == "win32":
        # psycopg async cannot run on the default ProactorEventLoop.
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    return asyncio.run(run_audit(args.days, args.sample, args.model, args.dry_run))


if __name__ == "__main__":
    sys.exit(main())
