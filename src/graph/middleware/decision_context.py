"""Decision context for the thread's model calls (#5678).

Two notes, both model-only (tagged guard notes; the operator's transcript never shows them):

* ``decision-context`` — an answered no-pause decision whose delivery turn never ran is
  added to the thread's messages once, sharing delivery claims with the chat driver.
* ``decision-open`` — while a no-pause decision is still pending on the thread, every model
  call's input carries a short note naming the question, so the agent neither forgets it
  nor lets someone other than the operator answer it. The note rides the request only
  (``request.override``), never the saved state, so it neither piles up in the checkpoint
  nor outlives the decision. When the caller is not the verified human operator (a
  federation peer, the fleet service token, an agent device) it adds that this caller
  cannot answer it. Pause-id decisions are untouched: their interrupt holds the turn.
"""

from __future__ import annotations

import asyncio
import logging

from langchain.agents.middleware import AgentMiddleware
from langgraph.config import get_config

from graph.decisions import decision_store
from graph.middleware.guard_notes import guard_note

logger = logging.getLogger(__name__)

OPEN_NOTE_GUARD = "decision-open"


def _thread_id() -> str | None:
    return (get_config().get("configurable") or {}).get("thread_id")


def caller_is_operator() -> bool:
    """Whether the turn's server-verified caller is the human operator.

    Reads only the principal authentication bound (never request metadata). A turn with
    no bound principal is local (a test, the SDK, a scheduled run) unless the server bound
    the federation trust tier; open mode keeps its single-operator meaning."""
    from a2a_impl.auth import is_human_operator, open_mode
    from graph.middleware.request_context import current_verified_principal
    from observability.tracing import current_trust_tier

    principal = current_verified_principal()
    if principal is None:
        return current_trust_tier() != "federation"
    if principal == "open/unknown":
        return open_mode()
    return is_human_operator(principal)


def open_decision_note(questions: list[str], *, operator: bool):
    """The ``decision-open`` note naming each still-open question."""
    lines = ["A decision you asked the operator is still open. Keep it in mind; "
             "do not answer it yourself or ask it again."]
    lines += [f"Question: {q}" for q in questions]
    if not operator:
        lines.append("The caller of this turn is not the operator and cannot answer it. "
                     "Do not ask this caller to answer or decide it.")
    return guard_note(OPEN_NOTE_GUARD, "\n".join(lines))


class DecisionContextMiddleware(AgentMiddleware):
    """Add ledger answers once, and name still-open no-pause decisions on every model call."""

    def before_model(self, state, runtime):  # type: ignore[override]
        try:
            thread_id = _thread_id()
            if not thread_id:
                return None
            store = decision_store()
            records = store.undelivered_answers(thread_id)
        except Exception:  # noqa: BLE001 — ledger availability must not fail a turn
            logger.warning("Could not read decision context", exc_info=True)
            return None

        notes = []
        for rec in records:
            answer = (f"Operator chose: {rec.option_label(rec.answer)} ({rec.answer})"
                      if rec.answer_kind == "option" else rec.answer)
            note = guard_note(
                "decision-context",
                f"The operator answered your decision.\nQuestion: {rec.question}\nAnswer: {answer}",
            )
            try:
                if store.claim_delivery(rec.id):
                    notes.append(note)
            except Exception:  # noqa: BLE001 — leave the answer available for a later turn
                logger.warning("Could not claim decision context for %s", rec.id, exc_info=True)
        return {"messages": notes} if notes else None

    async def abefore_model(self, state, runtime):  # type: ignore[override]
        return await asyncio.to_thread(self.before_model, state, runtime)

    def _open_note(self):
        """The open-decision note for this call, or None (none open, or an unreadable ledger)."""
        try:
            thread_id = _thread_id()
            if not thread_id:
                return None
            records = decision_store().pending_unpaused(thread_id)
            if not records:
                return None
            return open_decision_note([rec.question for rec in records], operator=caller_is_operator())
        except Exception:  # noqa: BLE001 — ledger availability must not fail a turn
            logger.warning("Could not read open decisions", exc_info=True)
            return None

    @staticmethod
    def _with_note(request, note):
        return request if note is None else request.override(messages=[*request.messages, note])

    def wrap_model_call(self, request, handler):  # type: ignore[override]
        return handler(self._with_note(request, self._open_note()))

    async def awrap_model_call(self, request, handler):  # type: ignore[override]
        note = await asyncio.to_thread(self._open_note)
        return await handler(self._with_note(request, note))
