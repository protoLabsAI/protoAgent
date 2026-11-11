"""Empty-at-cap retry — one more try for a turn that ended the loop with nothing to show.

ADR 0128 S3 (#4875). A model turn can end the agent loop carrying no usable output in two
shapes the completion guard already knew about but only rescued for subagents that
declared a deliverable:

  * the reply was CUT at the output cap (``finish_reason`` length / max_tokens) with no
    tool call and ≤200 chars of visible text — the model ran out of budget before it said
    anything usable;
  * the turn was REASONING-ONLY (#2642 / #3584, protoAgent#4108): the whole budget went to
    ``<think>`` and the content came back empty, often a degenerate loop closed by an EOS
    sampled mid-thought.

Either way a plain continuation replays the failure: the truncated / looping reasoning
round-trips back as the turn's ``reasoning_content`` with thinking still on, and the model
picks it up where it stopped. So the retry DROPS the failed turn's reasoning
(``without_reasoning``) and makes ONE more call with thinking OFF where the gateway slot
supports it (``thinking_off_settings``). One retry per turn, never a loop of them.

This runs on EVERY subagent (``CompletionGuardMiddleware`` only attaches when a deliverable
is declared), and on the lead chat stack only behind ``model.empty_retry_lead`` (off by
default — Josh's call: the lead stays unchanged unless the operator opts in).

The retry increments S0's ``llm_empty_completions_total{reason, recovered}`` counter, so
S0's measurement shows how often it salvaged a reply — ``reason`` is ``length`` or
``reasoning_only``, ``recovered`` is whether the one retry produced a usable turn.

Ordering (the #4875 memo): this middleware is registered BEFORE the completion guard, so
the guard's ``after_model`` runs FIRST (LangChain runs after_model in reverse registration
order) and defers only turns eligible for salvage here. Both use the same declared
contract predicate and note history; failed salvage resumes bounded contract recovery.
"""

from __future__ import annotations

import logging

from langchain.agents.middleware import AgentMiddleware, hook_config

from graph.middleware.completion_guard import (
    CompletionRecovery,
    describe_turn,
    thinking_off_settings,
    without_reasoning,
)
from graph.middleware.guard_notes import guard_note, is_guard_note

log = logging.getLogger(__name__)

RETRY_MARK = "[empty-at-cap]"  # leading tag on the injected note — for the model's eyes only
EMPTY_GUARD = "empty-at-cap"  # guard tag the note carries, so the retry can be recognised later
_REASON_KWARG = "protoagent_empty_reason"  # the triggering turn's reason, carried on the note


def _model_label(model) -> str:
    """The model name for the metric label, as other middleware read it (prompt_cache.py)."""
    return getattr(model, "model_name", None) or getattr(model, "model", "") or ""


class EmptyAtCapMiddleware(AgentMiddleware):
    """Retry ONCE, thinking off, a turn that ended the loop with no tool call and almost no
    text because it hit the output cap or spent itself thinking (ADR 0128 S3)."""

    def __init__(self, *, recovery: CompletionRecovery | None = None):
        super().__init__()
        # Latest call's model name, captured in wrap_model_call for the metric label.
        self._model = ""
        self._recovery = recovery or CompletionRecovery()

    def _intervene(self, state) -> dict | None:
        messages = state.get("messages") or []
        last = messages[-1] if messages else None
        reason = self._recovery.reason(last)
        if not self._recovery.should_salvage(messages):
            return None
        cause = "ran out of output budget" if reason == "length" else "spent itself thinking"
        note = (
            f"{RETRY_MARK} Your last turn ended with no tool call and almost no text because it "
            f"{cause}. Do not re-analyse. Answer now, concisely, from what you already have; if "
            "you were about to call a tool, make that call."
        )
        log.warning(
            "[empty-at-cap] empty turn (%s); retrying once with thinking off (%s)",
            reason,
            describe_turn(last),
        )
        retry_note = guard_note(EMPTY_GUARD, note)
        retry_note.additional_kwargs[_REASON_KWARG] = reason
        # Replace the failed turn (same id) with a reasoning-stripped copy so the loop is not
        # round-tripped back into the retry; only append the note when there is no id to
        # replace (then the reasoning cannot be dropped — mirrors the old guard behaviour).
        update = [without_reasoning(last)] if getattr(last, "id", None) else []
        return {"jump_to": "model", "messages": [*update, retry_note]}

    def _record(self, reason: str, recovered: bool) -> None:
        """Count the empty completion on S0's counter (ADR 0128). Best-effort — metrics
        must never fail a run, and are a no-op when the exporter is off."""
        try:
            from observability import metrics

            metrics.record_empty_completion(self._model, reason, recovered)
        except Exception:  # noqa: BLE001 — telemetry only
            log.debug("[empty-at-cap] metric record failed", exc_info=True)

    def _prepare(self, request):
        """Capture the model name for the metric, and turn thinking off for the retry call —
        the call whose last message is our own note (and only where the slot supports it)."""
        self._model = _model_label(getattr(request, "model", None)) or self._model
        messages = getattr(request, "messages", None) or []
        if not messages or not is_guard_note(messages[-1], EMPTY_GUARD):
            return request
        settings = thinking_off_settings(request.model, request.model_settings)
        return request if settings is None else request.override(model_settings=settings)

    def _record_result(self, request, response):
        # Record at the model boundary: a completion-guard jump skips later
        # after_model hooks, including ours, when salvage still owes a contract.
        messages = request.messages or []
        if messages and is_guard_note(messages[-1], EMPTY_GUARD):
            reason = messages[-1].additional_kwargs.get(_REASON_KWARG) or "unknown"
            last = response.result[-1] if response.result else None
            recovered = self._recovery.recovered(last)
            self._record(reason, recovered)
            if recovered:
                log.info("[empty-at-cap] retry recovered (%s)", reason)
            else:
                log.warning(
                    "[empty-at-cap] retry unsuccessful (%s); salvage exhausted (%s)",
                    reason, describe_turn(last),
                )
        return response

    def wrap_model_call(self, request, handler):  # type: ignore[override]
        return self._record_result(request, handler(self._prepare(request)))

    async def awrap_model_call(self, request, handler):  # type: ignore[override]
        return self._record_result(request, await handler(self._prepare(request)))

    @hook_config(can_jump_to=["model"])
    def after_model(self, state, runtime):  # type: ignore[override]
        return self._intervene(state)

    @hook_config(can_jump_to=["model"])
    async def aafter_model(self, state, runtime):  # type: ignore[override]
        return self._intervene(state)
