"""Live commentary engine, HITL voice-approval gateway, and the bridge that
connects a user's agent handler to the voice proxy.

State machine (per session)::

    LISTENING --final transcript--> THINKING --agent streams text--> SPEAKING
        ^                              |                                |
        |                              | pulse(requires_approval)       |
        |                              v                                |
        +------- result spoken --- AWAITING_APPROVAL <---- barge-in ----+

While AWAITING_APPROVAL the pulse gate is closed: background commentary is
paused, the approval prompt is spoken with ``interrupt=True``, and the next
final transcript resolves the approval instead of starting a new agent turn.
"""

from __future__ import annotations

import asyncio
import contextlib
import inspect
import logging
import re
import time
from collections.abc import AsyncIterator, Callable
from typing import TYPE_CHECKING, Any

from .models import (
    ApprovalDecision,
    ApprovalRequest,
    ApprovalResult,
    GuardrailVerdict,
    SessionConfig,
    SessionState,
    ToolStatusPulse,
    ToolStepStatus,
)

if TYPE_CHECKING:
    from .base import BaseVoiceAgentProxy
    from .guardrails_and_eval import GuardrailPipeline, TelemetryRecorder

logger = logging.getLogger("voiceagent")


# ---------------------------------------------------------------------------
# Agent handler contract
# ---------------------------------------------------------------------------

# What users write: an async function taking AgentContext and returning a
# final string, an async iterator of text chunks, or None (if it spoke via
# ctx.say itself).  Adapters normalize LangChain/CrewAI/WS/HTTP to this.
AgentHandler = Callable[["AgentContext"], Any]


class AgentContext:
    """Handed to the user's agent function for each utterance.

    The whole "personalize in a few lines" DX lives here::

        async def my_agent(ctx):
            await ctx.status("fetching_db_records")
            if not await ctx.approve("Delete 45 records?"):
                return "Cancelled."
            return "Done!"
    """

    def __init__(self, bridge: OrchestratorBridge, text: str) -> None:
        self._bridge = bridge
        self.text: str = text  # the (guardrail-masked) user utterance
        self.config: SessionConfig = bridge.config
        self.history: list[dict[str, str]] = bridge.history

    async def status(
        self,
        step: str,
        detail: str | None = None,
        status: ToolStepStatus = ToolStepStatus.PROCESSING,
    ) -> None:
        """Emit an intermediate step pulse -> spoken live commentary."""
        await self._bridge.emit_pulse(
            ToolStatusPulse(status=status, step=step, detail=detail)
        )

    async def say(self, text: str) -> None:
        """Speak immediately, bypassing commentary coalescing."""
        await self._bridge.speak(text)

    async def approve(
        self,
        prompt: str,
        payload: dict[str, Any] | None = None,
        timeout_s: float | None = None,
    ) -> bool:
        """Blocking voice approval; True only on an explicit verbal yes."""
        result = await self._bridge.request_approval(
            ApprovalRequest(
                step="agent_approval",
                prompt_text=prompt,
                payload=payload or {},
                timeout_s=timeout_s or self.config.approval_timeout_s,
            )
        )
        return result.decision is ApprovalDecision.APPROVED

    async def emit(self, pulse: ToolStatusPulse) -> ApprovalResult | None:
        """Full pulse contract: approval pulses block and return the result."""
        return await self._bridge.emit_pulse(pulse)


# ---------------------------------------------------------------------------
# Commentary engine
# ---------------------------------------------------------------------------

# Templates per (language, tone) per status. `{step}` receives the humanized
# step name; `detail` is appended by render() when present.
_TEMPLATES: dict[tuple[str, str], dict[ToolStepStatus, list[str]]] = {
    ("en", "neutral"): {
        ToolStepStatus.STARTED: ["Starting {step}.", "Beginning {step} now."],
        ToolStepStatus.PROCESSING: [
            "I'm {step} now.",
            "Currently {step}.",
            "Still working — {step}.",
        ],
        ToolStepStatus.COMPLETED: ["Done {step}.", "Finished {step}."],
        ToolStepStatus.FAILED: ["Something went wrong while {step}."],
        ToolStepStatus.AWAITING_APPROVAL: ["I need your approval before {step}."],
    },
    ("en", "warm"): {
        ToolStepStatus.STARTED: ["Alright, let me get started on {step}!"],
        ToolStepStatus.PROCESSING: [
            "Just a moment — I'm {step} for you.",
            "Hang tight, {step} right now.",
        ],
        ToolStepStatus.COMPLETED: ["Great news — finished {step}!"],
        ToolStepStatus.FAILED: ["Oh no, I hit a snag while {step}. Let me see."],
        ToolStepStatus.AWAITING_APPROVAL: [
            "Quick check before I continue — I need your okay for {step}."
        ],
    },
    ("en", "formal"): {
        ToolStepStatus.STARTED: ["Commencing {step}."],
        ToolStepStatus.PROCESSING: ["Please hold while I am {step}."],
        ToolStepStatus.COMPLETED: ["{step} has been completed."],
        ToolStepStatus.FAILED: ["An error occurred during {step}."],
        ToolStepStatus.AWAITING_APPROVAL: ["Your authorization is required for {step}."],
    },
    ("hi", "neutral"): {
        ToolStepStatus.STARTED: ["{step} शुरू कर रहा हूँ।"],
        ToolStepStatus.PROCESSING: ["अभी {step} चल रहा है, एक पल रुकिए।", "मैं {step} कर रहा हूँ।"],
        ToolStepStatus.COMPLETED: ["{step} पूरा हो गया।"],
        ToolStepStatus.FAILED: ["{step} में कुछ गड़बड़ हो गई।"],
        ToolStepStatus.AWAITING_APPROVAL: ["{step} से पहले मुझे आपकी अनुमति चाहिए।"],
    },
    ("es", "neutral"): {
        ToolStepStatus.STARTED: ["Comenzando {step}."],
        ToolStepStatus.PROCESSING: ["Estoy {step} ahora, un momento."],
        ToolStepStatus.COMPLETED: ["Terminé {step}."],
        ToolStepStatus.FAILED: ["Algo salió mal durante {step}."],
        ToolStepStatus.AWAITING_APPROVAL: ["Necesito tu aprobación para {step}."],
    },
}

_ERROR_LINES = {
    "en": "Sorry, something went wrong on my side. Please try again.",
    "hi": "माफ़ कीजिए, मेरी तरफ़ से कुछ गड़बड़ हो गई। कृपया दोबारा कोशिश करें।",
    "es": "Perdona, algo salió mal por mi parte. Inténtalo de nuevo.",
}


_NO_ARTICLE_AFTER = {
    "the", "a", "an", "that", "this", "those", "these", "your", "my", "our",
    "their", "his", "her", "its", "it", "them", "up", "out", "in", "on",
    "to", "for", "with", "at", "over", "all", "some",
}


def humanize_step(step: str) -> str:
    """``fetching_db_records`` -> ``fetching the db records`` (speakable)."""
    words = re.sub(r"[_\-]+", " ", step).strip().split()
    if not words:
        return "that step"
    # Insert an article after a leading gerund ("fetching db records" ->
    # "fetching the db records") unless the next word already reads fine
    # without one ("looking that up", "checking your order").
    if len(words) > 1 and words[0].endswith("ing") and words[1] not in _NO_ARTICLE_AFTER:
        words.insert(1, "the")
    return " ".join(words)


class CommentaryEngine:
    """Maps orchestrator pulses to short spoken fragments, with coalescing.

    Coalescing: a chatty orchestrator can pulse every 100 ms; speech runs at
    ~2.5 words/sec.  Without a governor the audio queue becomes a backlog of
    stale narration.  We speak a pulse immediately if the last commentary was
    >= ``commentary_min_gap_s`` ago; otherwise we stash it as *pending* and a
    timer speaks only the LATEST pending pulse when the gap expires.
    """

    def __init__(self, proxy: BaseVoiceAgentProxy, config: SessionConfig) -> None:
        self._proxy = proxy
        self._config = config
        self._lang = config.language.split("-")[0].lower()
        self._rotation: dict[tuple[str, ToolStepStatus], int] = {}
        self._last_spoken = 0.0
        self._pending: ToolStatusPulse | None = None
        self._flush_task: asyncio.Task[None] | None = None

    @classmethod
    def register_templates(
        cls,
        language: str,
        tone: str,
        templates: dict[ToolStepStatus, list[str]],
    ) -> None:
        """Extension point: add/override commentary for a language+tone."""
        _TEMPLATES[(language.split("-")[0].lower(), tone)] = templates

    def _template_bank(self) -> dict[ToolStepStatus, list[str]]:
        for key in (
            (self._lang, self._config.tone),
            (self._lang, "neutral"),
            ("en", self._config.tone),
            ("en", "neutral"),
        ):
            if key in _TEMPLATES:
                return _TEMPLATES[key]
        return _TEMPLATES[("en", "neutral")]

    def render(self, pulse: ToolStatusPulse) -> str:
        bank = self._template_bank()
        options = bank.get(pulse.status) or bank[ToolStepStatus.PROCESSING]
        # Deterministic rotation so repeated pulses for the same step don't
        # repeat the same sentence verbatim (sounds robotic).
        rot_key = (pulse.step, pulse.status)
        idx = self._rotation.get(rot_key, 0)
        self._rotation[rot_key] = idx + 1
        line = options[idx % len(options)].format(step=humanize_step(pulse.step))
        if pulse.detail:
            line = f"{line} {pulse.detail}"
        return line

    def error_line(self) -> str:
        return _ERROR_LINES.get(self._lang, _ERROR_LINES["en"])

    async def on_pulse(self, pulse: ToolStatusPulse) -> None:
        now = time.monotonic()
        if now - self._last_spoken >= self._config.commentary_min_gap_s:
            await self._speak(pulse)
            return
        # Within the gap: replace any pending pulse (latest wins) and make
        # sure exactly one flush timer is running.
        self._pending = pulse
        if self._flush_task is None or self._flush_task.done():
            delay = self._config.commentary_min_gap_s - (now - self._last_spoken)
            self._flush_task = asyncio.create_task(self._flush_later(delay))

    async def _flush_later(self, delay: float) -> None:
        await asyncio.sleep(max(0.0, delay))
        pulse, self._pending = self._pending, None
        if pulse is not None:
            await self._speak(pulse)

    async def _speak(self, pulse: ToolStatusPulse) -> None:
        self._last_spoken = time.monotonic()
        await self._proxy.speak_text(self.render(pulse))

    def speak_cached_filler(self) -> bool:
        """TTFB≈0 path: enqueue a pre-synthesized filler if one is cached.

        Returns True if raw PCM was enqueued (split-stack engines warm the
        cache at startup); False means the caller should fall back to TTS.
        """
        from .base import DEFAULT_FILLER_PHRASES  # local import avoids cycle

        for phrase in DEFAULT_FILLER_PHRASES.get(
            self._lang, DEFAULT_FILLER_PHRASES["en"]
        ):
            pcm = self._proxy.phrase_cache.get(
                phrase, self._config.language, self._config.voice_id
            )
            if pcm:
                self._proxy.enqueue_audio(pcm)
                self._last_spoken = time.monotonic()
                return True
        return False

    def cancel_pending(self) -> None:
        self._pending = None
        if self._flush_task is not None:
            self._flush_task.cancel()
            self._flush_task = None


# ---------------------------------------------------------------------------
# Approval gateway
# ---------------------------------------------------------------------------

_YES_LEXICON: dict[str, set[str]] = {
    "en": {
        "yes", "yeah", "yep", "yup", "sure", "proceed", "confirm", "confirmed",
        "approved", "approve", "ok", "okay", "affirmative", "go", "ahead", "do",
    },
    "hi": {"हाँ", "हां", "जी", "ठीक", "करो", "बढ़ो", "मंज़ूर", "मंजूर", "हाँजी"},
    "es": {"sí", "si", "claro", "adelante", "procede", "dale", "confirmo", "vale"},
}
_NO_LEXICON: dict[str, set[str]] = {
    "en": {
        "no", "nope", "stop", "abort", "cancel", "don't", "dont", "negative",
        "reject", "rejected", "never", "wait",
    },
    "hi": {"नहीं", "ना", "मत", "रुको", "रद्द", "नही"},
    "es": {"no", "para", "cancela", "detente", "nunca", "espera"},
}

_APPROVAL_REPROMPT = {
    "en": "Sorry, I didn't catch that. Please say yes to proceed or no to abort.",
    "hi": "माफ़ कीजिए, समझ नहीं आया। आगे बढ़ने के लिए 'हाँ' और रोकने के लिए 'नहीं' कहिए।",
    "es": "Perdona, no te entendí. Di sí para continuar o no para cancelar.",
}
_APPROVAL_TIMEOUT_LINE = {
    "en": "I didn't hear a response, so I'm cancelling that action to be safe.",
    "hi": "कोई जवाब नहीं मिला, इसलिए सुरक्षा के लिए मैं यह काम रद्द कर रहा हूँ।",
    "es": "No escuché respuesta, así que cancelo la acción por seguridad.",
}

_WORD_RE = re.compile(r"[\wऀ-ॿ']+", re.UNICODE)


def classify_verbal_response(text: str, language: str) -> ApprovalDecision:
    """Map a transcript to APPROVED / REJECTED / AMBIGUOUS.

    Safety bias: any negative token vetoes — "no wait yes" rejects.  A wrong
    rejection costs a retry; a wrong approval executes an unapproved action.
    """
    lang = language.split("-")[0].lower()
    yes = _YES_LEXICON.get(lang, set()) | _YES_LEXICON["en"]
    no = _NO_LEXICON.get(lang, set()) | _NO_LEXICON["en"]
    tokens = {tok.lower() for tok in _WORD_RE.findall(text)}
    has_yes = bool(tokens & yes)
    has_no = bool(tokens & no)
    if has_no:
        return ApprovalDecision.REJECTED
    if has_yes:
        return ApprovalDecision.APPROVED
    return ApprovalDecision.AMBIGUOUS


class ApprovalGateway:
    """Blocking voice-approval mechanism.

    ``request`` halts background pulse consumption (the gate), speaks the
    prompt, waits for the user's verbal verdict, and returns a clean
    ApprovalResult.  Timeouts and double-ambiguity resolve to safe rejection.
    """

    def __init__(
        self,
        proxy: BaseVoiceAgentProxy,
        config: SessionConfig,
        telemetry: TelemetryRecorder | None = None,
    ) -> None:
        self._proxy = proxy
        self._config = config
        self._telemetry = telemetry
        self._lang = config.language.split("-")[0].lower()
        # Open by default; cleared while an approval is in flight so pulse
        # commentary can't talk over the approval dialog.
        self.gate = asyncio.Event()
        self.gate.set()

    async def request(self, req: ApprovalRequest) -> ApprovalResult:
        started = time.monotonic()
        self.gate.clear()
        self._proxy.set_state(SessionState.AWAITING_APPROVAL)
        try:
            decision, transcript = await self._converse(req)
        finally:
            self.gate.set()
        result = ApprovalResult(
            request_id=req.request_id,
            decision=decision,
            transcript=transcript,
            latency_ms=(time.monotonic() - started) * 1000.0,
        )
        if self._telemetry is not None:
            self._telemetry.log_event(
                "approval.result",
                {"decision": decision.value, "step": req.step, "transcript": transcript},
            )
        return result

    async def _converse(self, req: ApprovalRequest) -> tuple[ApprovalDecision, str | None]:
        # interrupt=True: an approval prompt must never queue behind ongoing
        # commentary — the user has to hear the question NOW.
        await self._proxy.speak_text(req.prompt_text, interrupt=True)
        transcript = await self._proxy.open_listen_window(req.timeout_s)
        if transcript is None:
            await self._proxy.speak_text(
                _APPROVAL_TIMEOUT_LINE.get(self._lang, _APPROVAL_TIMEOUT_LINE["en"])
            )
            return ApprovalDecision.TIMEOUT, None

        decision = classify_verbal_response(transcript, self._config.language)
        if decision is not ApprovalDecision.AMBIGUOUS:
            return decision, transcript

        # Exactly one clarification round; second ambiguity = safe reject.
        await self._proxy.speak_text(
            _APPROVAL_REPROMPT.get(self._lang, _APPROVAL_REPROMPT["en"]),
            interrupt=True,
        )
        retry = await self._proxy.open_listen_window(req.timeout_s)
        if retry is None:
            return ApprovalDecision.TIMEOUT, transcript
        decision = classify_verbal_response(retry, self._config.language)
        if decision is ApprovalDecision.AMBIGUOUS:
            return ApprovalDecision.AMBIGUOUS, retry
        return decision, retry


# ---------------------------------------------------------------------------
# Orchestrator bridge
# ---------------------------------------------------------------------------

_SENTENCE_BOUNDARY = re.compile(r"[.!?;:।]\s*$")


class OrchestratorBridge:
    """Routes traffic between the voice proxy, guardrails, commentary,
    approvals, and the user's agent handler."""

    def __init__(
        self,
        proxy: BaseVoiceAgentProxy,
        handler: AgentHandler,
        config: SessionConfig,
        guardrails: GuardrailPipeline | None = None,
        telemetry: TelemetryRecorder | None = None,
    ) -> None:
        self.proxy = proxy
        self.handler = handler
        self.config = config
        self.guardrails = guardrails
        self.telemetry = telemetry
        self.commentary = CommentaryEngine(proxy, config)
        self.approvals = ApprovalGateway(proxy, config, telemetry)
        self.history: list[dict[str, str]] = []
        self._agent_task: asyncio.Task[None] | None = None
        # Approval results waiting to be forwarded upstream (external
        # orchestrator adapters drain this).
        self.upstream_outbox: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=32)

    # -- inbound: user spoke ---------------------------------------------------

    async def handle_user_utterance(self, text: str) -> None:
        """Guardrail-check the transcript, then run the agent on it.

        Spawns (does not await) the agent task: this is called from the
        engine's event loop, and a 30-second orchestrator run must never
        stall audio processing.
        """
        if self.telemetry is not None:
            self.telemetry.log_event("user.utterance", {"text": text})

        if self.guardrails is not None:
            outcome = await self.guardrails.check(text)
            if outcome.verdict is GuardrailVerdict.BLOCK:
                await self.proxy.speak_text(
                    outcome.refusal_text or self.commentary.error_line()
                )
                return
            if outcome.verdict is GuardrailVerdict.MASK:
                text = outcome.text  # PII already masked in place

        # A new utterance supersedes any still-running agent turn.
        if self._agent_task is not None and not self._agent_task.done():
            self._agent_task.cancel()
        self.proxy.set_state(SessionState.THINKING)
        self._agent_task = asyncio.create_task(self._run_agent(text), name="agent-turn")

    async def _run_agent(self, text: str) -> None:
        self.history.append({"role": "user", "content": text})
        ctx = AgentContext(self, text)
        span = self.telemetry.span("agent.turn") if self.telemetry else None
        try:
            result = self.handler(ctx)
            if inspect.isawaitable(result):
                result = await result
            if result is None:
                return
            if isinstance(result, str):
                self.history.append({"role": "assistant", "content": result})
                await self.speak(result)
                return
            if hasattr(result, "__aiter__"):
                await self._speak_stream(result)
                return
            logger.warning("agent handler returned unsupported type %r", type(result))
        except asyncio.CancelledError:
            # Barge-in or superseding utterance — silence is the right output.
            raise
        except Exception:
            logger.exception("agent handler failed")
            await self.speak(self.commentary.error_line())
        finally:
            if span is not None:
                span.end()

    async def _speak_stream(self, chunks: AsyncIterator[str]) -> None:
        """Speak a token stream with the lowest-latency path available.

        Preferred: hand the raw token iterator to the engine's
        ``speak_token_stream`` (split-stack pipes it into a TTS continuation
        context — first 3-5 words hit TTS immediately).  Fallback: buffer to
        clause boundaries (or 24 words for run-on generations) and speak
        clause-by-clause, so the first clause plays while the rest streams.
        """
        full: list[str] = []

        async def recorded() -> AsyncIterator[str]:
            async for chunk in chunks:
                if chunk:
                    full.append(chunk)
                    yield chunk

        stream = recorded()
        await self.approvals.gate.wait()
        if not await self.proxy.speak_token_stream(stream):
            buffer: list[str] = []
            word_count = 0
            async for chunk in stream:
                buffer.append(chunk)
                word_count += chunk.count(" ") + 1
                joined = "".join(buffer)
                if _SENTENCE_BOUNDARY.search(joined) or word_count >= 24:
                    await self.speak(joined)
                    buffer.clear()
                    word_count = 0
            if buffer:
                await self.speak("".join(buffer))
        if full:
            self.history.append({"role": "assistant", "content": "".join(full)})

    async def run_agent_collect(self, text: str) -> str:
        """Run the handler and RETURN its text instead of speaking it.

        Used by model-fronted engines (Gemini Live), where the realtime model
        voices the final answer itself via a tool response.  Status pulses
        and approvals raised by the handler still flow through the normal
        spoken path while this call is in flight.
        """
        if self.guardrails is not None:
            outcome = await self.guardrails.check(text)
            if outcome.verdict is GuardrailVerdict.BLOCK:
                return outcome.refusal_text or self.commentary.error_line()
            if outcome.verdict is GuardrailVerdict.MASK:
                text = outcome.text
        self.history.append({"role": "user", "content": text})
        ctx = AgentContext(self, text)
        try:
            result = self.handler(ctx)
            if inspect.isawaitable(result):
                result = await result
            if result is None:
                final = ""
            elif isinstance(result, str):
                final = result
            elif hasattr(result, "__aiter__"):
                final = "".join([chunk async for chunk in result])
            else:
                final = str(result)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("agent handler failed (collect mode)")
            return self.commentary.error_line()
        if final:
            self.history.append({"role": "assistant", "content": final})
        return final

    # -- outbound helpers ---------------------------------------------------------

    async def speak(self, text: str) -> None:
        text = text.strip()
        if not text:
            return
        # Never talk over an in-flight approval dialog.
        await self.approvals.gate.wait()
        await self.proxy.speak_text(text)

    async def emit_pulse(self, pulse: ToolStatusPulse) -> ApprovalResult | None:
        """Entry point for both ctx.status() and external orchestrator pulses."""
        if self.telemetry is not None:
            self.telemetry.log_event(
                "pulse", {"step": pulse.step, "status": pulse.status.value}
            )
        if pulse.requires_approval or pulse.status is ToolStepStatus.AWAITING_APPROVAL:
            request = ApprovalRequest(
                step=pulse.step,
                prompt_text=pulse.detail
                or self.commentary.render(
                    pulse.model_copy(update={"status": ToolStepStatus.AWAITING_APPROVAL})
                ),
                payload=pulse.approval_payload or {},
                timeout_s=self.config.approval_timeout_s,
            )
            return await self.request_approval(request)
        await self.approvals.gate.wait()
        await self.commentary.on_pulse(pulse)
        return None

    async def request_approval(self, request: ApprovalRequest) -> ApprovalResult:
        result = await self.approvals.request(request)
        # Also queue the clean JSON payload for upstream consumers (external
        # orchestrator adapters forward it over their own channel).
        try:
            self.upstream_outbox.put_nowait(result.to_upstream())
        except asyncio.QueueFull:
            logger.warning("upstream outbox full; dropping approval echo")
        return result

    async def on_barge_in(self) -> None:
        self.commentary.cancel_pending()
        if (
            self.config.cancel_agent_on_barge_in
            and self._agent_task is not None
            and not self._agent_task.done()
        ):
            self._agent_task.cancel()

    async def aclose(self) -> None:
        if self._agent_task is not None and not self._agent_task.done():
            self._agent_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._agent_task
