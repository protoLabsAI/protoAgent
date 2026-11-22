"""Recover answered no-pause decisions before the thread's next model call."""

from __future__ import annotations

import asyncio
import logging

from langchain.agents.middleware import AgentMiddleware
from langgraph.config import get_config

from graph.decisions import decision_store
from graph.middleware.guard_notes import guard_note

logger = logging.getLogger(__name__)


class DecisionContextMiddleware(AgentMiddleware):
    """Add ledger answers once, sharing delivery claims with the chat driver."""

    def before_model(self, state, runtime):  # type: ignore[override]
        try:
            thread_id = (get_config().get("configurable") or {}).get("thread_id")
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
