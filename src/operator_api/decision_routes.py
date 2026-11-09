"""Operator "decision needed" routes (#4725, item 3).

The read + answer surface over the durable decisions ledger (``graph/decisions.py``). When
the agent calls ``request_decision`` it parks the turn — even on an unattended autonomous
turn — and the server records a pending decision. The console (card 3) lists pending ones
here, shows the question + options, and answers one; answering resumes the parked turn
through the same HITL-resume entry point an attended session uses.

Registrar-style (``register_decision_routes(app)``), mounted next to the other operator API
registrars in ``server/__init__.py``. Full-scope operator auth, like the directive routes —
nothing here is a credential-plane write, so the global auth middleware gates it.

This file must NOT import ``server`` (the import-layering invariant): the parked-turn resume
goes over the loopback A2A endpoint (a network call, carrying the console's own
``hitl_resume`` marker), not through an in-process ``server`` import.
"""

from __future__ import annotations

import logging

from pydantic import BaseModel

log = logging.getLogger("protoagent.server")

#: What ``request_decision`` returns to the model when the operator DISMISSES a decision
#: instead of choosing an option. The cancel route resumes the parked turn with this so the
#: checkpoint interrupt is cleared (the turn stops waiting); the ledger record is already
#: marked ``cancelled``, so the resume only unblocks the thread.
CANCELLED_RESUME = (
    "[decision dismissed by the operator] The operator cancelled this decision without "
    "choosing an option. Do not take the action you were deciding on — acknowledge that it "
    "was cancelled and end your turn."
)


class AnswerRequest(BaseModel):
    """The operator's answer: a defined option by id (validated against the record's options)
    or a free-text answer. At least one is required."""

    option_id: str | None = None
    text: str | None = None


def _resolve_answer(rec, body: AnswerRequest) -> str:
    """The answer string to resume the turn with, validated against the record's options.

    An ``option_id`` must name one of the decision's options (matched by id, then by label)
    and resolves to that option's canonical id; a free-text ``text`` is taken verbatim. Raises
    :class:`fastapi.HTTPException` 400 on an unknown option or when neither field is given."""
    from fastapi import HTTPException

    option_id = (body.option_id or "").strip()
    text = (body.text or "").strip()
    if option_id:
        by_id = {str(o.get("id")): str(o.get("id")) for o in rec.options if isinstance(o, dict) and o.get("id")}
        by_label = {str(o.get("label")): str(o.get("id")) for o in rec.options if isinstance(o, dict) and o.get("id")}
        resolved = by_id.get(option_id) or by_label.get(option_id)
        if resolved is None:
            valid = ", ".join(by_id) or "(none)"
            raise HTTPException(status_code=400, detail=f"unknown option {option_id!r} — valid options: {valid}")
        return resolved
    if text:
        return text
    raise HTTPException(status_code=400, detail="provide an option_id or free-text answer")


async def _resume_parked_turn(session_id: str, answer: str, decision_id: str | None = None) -> bool:
    """Resume the turn parked on this decision with ``answer``, over the loopback A2A
    endpoint — the SAME HITL-resume entry point the console uses.

    With a ``decision_id`` (the answer path) the id is checked against the checkpoint
    interrupt under the turn lock, and the turn driver closes the record only after the
    checkpoint consumes the answer. WITHOUT one (the cancel path, which has already closed
    the record) the check is skipped — the resume only has to CLEAR the lingering checkpoint
    interrupt so the session isn't wedged for later autonomous turns. Delivery failure
    leaves the interrupt in place; the operator can retry, and the autonomous hold refuses
    to park on a non-pending decision anyway, so a failed clear can never block the session.
    """
    import uuid

    import httpx

    from a2a_impl.auth import self_invocation_headers
    from runtime.state import STATE

    port = getattr(STATE, "active_port", None)
    if not port:
        log.warning("[decision] no active port — cannot resume parked turn for %s", session_id)
        return False
    headers = {"Content-Type": "application/json", "A2A-Version": "1.0"}
    headers.update(self_invocation_headers())
    message_id = str(uuid.uuid4())
    # The operator is answering/dismissing: a live human, so an empty origin (interactive) —
    # `hitl_resume` converts this to a graph resume rather than a fresh turn. A `decision_id`
    # is carried only on the answer path (it gates validate_decision_resume); the cancel path
    # omits it so the resume just clears the interrupt on the already-closed record.
    metadata = {"hitl_resume": True}
    if decision_id:
        metadata["decision_id"] = decision_id
    body = {
        "jsonrpc": "2.0",
        "id": message_id,
        "method": "SendMessage",
        "params": {
            "message": {
                "role": "ROLE_USER",
                "parts": [{"text": answer}],
                "messageId": message_id,
                "contextId": session_id,
                "metadata": metadata,
            },
        },
    }
    try:
        async with httpx.AsyncClient(timeout=120) as client:
            r = await client.post(f"http://127.0.0.1:{port}/a2a", headers=headers, json=body)
        if r.status_code >= 400:
            log.warning("[decision] resume for %s failed: HTTP %d %s", session_id, r.status_code, r.text[:200])
            return False
        return not bool(r.json().get("error"))
    except Exception:  # noqa: BLE001 — a resume transport error must not 500 the answer route
        log.exception("[decision] resume POST failed for %s", session_id)
        return False


def register_decision_routes(app) -> None:
    """Register the ``/api/decisions`` routes on ``app`` (#4725)."""
    import asyncio

    from fastapi import HTTPException

    from graph.decisions import (
        STATES,
        DecisionClosed,
        DecisionNotFound,
        decision_store,
    )

    @app.get("/api/decisions")
    async def _api_decisions(state: str | None = None, session_id: str | None = None, limit: int = 200):
        """Decisions newest first — ``{"decisions": [...], "pending": N}``.

        ``?state=pending|answered|expired|cancelled`` narrows the list (the console shows the
        inbox of ``pending`` ones); ``?session_id=`` scopes to one chat."""
        if state is not None and state not in STATES:
            raise HTTPException(status_code=400, detail=f"state must be one of {', '.join(STATES)}")
        store = decision_store()
        items = await asyncio.to_thread(
            store.list, state=state, session_id=(session_id or None), limit=min(max(1, int(limit)), 500)
        )
        pending = await asyncio.to_thread(store.list, state="pending", limit=500)
        return {"decisions": [d.to_dict() for d in items], "pending": len(pending)}

    @app.get("/api/decisions/{decision_id}")
    async def _api_decision(decision_id: str):
        """One decision by id, or 404."""
        rec = await asyncio.to_thread(decision_store().get, decision_id)
        if rec is None:
            raise HTTPException(status_code=404, detail=f"no decision {decision_id!r}")
        return rec.to_dict()

    @app.post("/api/decisions/{decision_id}/answer")
    async def _api_answer_decision(decision_id: str, body: AnswerRequest):
        """Answer a pending decision and resume its parked turn.

        Validates the answer against the record's options (400 on an unknown option or an
        empty body). Closed decisions return 409, unknown ids 404. The A2A resume
        closes the record after consuming the interrupt; failed delivery is retryable."""
        store = decision_store()
        rec = await asyncio.to_thread(store.get, decision_id)
        if rec is None:
            raise HTTPException(status_code=404, detail=f"no decision {decision_id!r}")
        answer = _resolve_answer(rec, body)
        if not rec.pending:
            raise HTTPException(status_code=409, detail=f"decision {decision_id} is already {rec.state}")
        await _resume_parked_turn(rec.session_id, answer, decision_id)
        updated = await asyncio.to_thread(store.get, decision_id)
        # The checkpoint is authoritative even if the HTTP response was lost or timed out.
        return {"decision": updated.to_dict(), "resumed": updated.state == "answered"}

    @app.post("/api/decisions/{decision_id}/cancel")
    async def _api_cancel_decision(decision_id: str):
        """Cancel a pending decision (the operator dismissed it) and unblock the parked turn.

        404 for an unknown id, 409 when it is no longer pending. The record is marked
        cancelled (recording the operator's intent + publishing ``decision.changed``), then
        the parked checkpoint interrupt is CLEARED by resuming the turn with a cancellation
        sentinel — so the session is NOT left blocked for later autonomous (scheduler /
        inbox / webhook) turns. Clearing is best-effort: the autonomous hold also refuses to
        park on a non-pending decision, so a failed clear can never wedge the session."""
        try:
            updated = await asyncio.to_thread(decision_store().cancel, decision_id)
        except DecisionNotFound as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except DecisionClosed as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        await _resume_parked_turn(updated.session_id, CANCELLED_RESUME)
        return {"decision": updated.to_dict()}
