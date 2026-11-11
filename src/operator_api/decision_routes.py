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

from fastapi import Request
from pydantic import BaseModel

from graph.decision_resume import CANCELLED_RESUME as CANCELLED_RESUME
from graph.decision_resume import resume_parked_turn as _resume_parked_turn


class AnswerRequest(BaseModel):
    """The operator's answer: a defined option by id (validated against the record's options)
    or a free-text answer. At least one is required."""

    option_id: str | None = None
    text: str | None = None
    surface: str = "console"


class CancelRequest(BaseModel):
    reason: str = ""


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
        """Pending decisions by expiry, then undated oldest first — ``{"decisions": [...], "pending": N}``.

        ``?state=pending|answered|expired|cancelled|withdrawn`` narrows the list (the console shows the
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

    # Answers being delivered right now, by decision id. Two surfaces answering the same
    # decision at once both used to send a resume: the loser's resume was refused under the
    # turn lock, but only after its task and chat message existed, and the route still said
    # 200 "Sent" (release acceptance bd-nfd6g.17, D3). A second answer now waits for the one in
    # flight to settle instead of racing it. Single-process state: one server owns the ledger.
    inflight: dict[str, asyncio.Future] = {}
    # The winner's record closes only when its resumed graph run finishes, so wait out the
    # resume's own transport timeout (120 s, graph/decision_resume.py) before giving up.
    settle_wait_s = 130.0

    @app.post("/api/decisions/{decision_id}/answer")
    async def _api_answer_decision(decision_id: str, body: AnswerRequest, request: Request):
        """Answer a pending decision and resume its parked turn.

        Validates the answer against the record's options (400 on an unknown option or an
        empty body). Closed decisions return 409, unknown ids 404. The A2A resume
        closes the record after consuming the interrupt; failed delivery is retryable."""
        store = decision_store()
        rec = await asyncio.to_thread(store.get, decision_id)
        if rec is None:
            raise HTTPException(status_code=404, detail=f"no decision {decision_id!r}")
        if not rec.pending:
            raise HTTPException(status_code=409, detail=rec.to_dict())
        answer = _resolve_answer(rec, body)
        loop = asyncio.get_running_loop()
        deadline = loop.time() + settle_wait_s
        while (other := inflight.get(decision_id)) is not None:
            # Another answer is mid-delivery. If it closes the decision, ours lost: 409 with the
            # real record. If it fails delivery (still pending), take our turn to deliver.
            remaining = deadline - loop.time()
            if remaining <= 0:
                raise HTTPException(status_code=409, detail=rec.to_dict())
            try:
                await asyncio.wait_for(asyncio.shield(other), timeout=min(remaining, 0.25))
            except asyncio.TimeoutError:
                pass
            rec = await asyncio.to_thread(store.get, decision_id)
            if rec is None or not rec.pending:
                raise HTTPException(status_code=409, detail=rec.to_dict() if rec else None)
        mine = loop.create_future()
        inflight[decision_id] = mine
        text = f"Operator chose: {rec.option_label(answer)} ({answer})" if body.option_id else answer
        actor = getattr(request.state, "device_id", None) or "operator"
        try:
            # Its result isn't needed: the ledger, re-read below, says whose answer landed.
            await _resume_parked_turn(rec.session_id, text, decision_id, resolution={
                "answer": answer, "answered_by": actor,
                "answered_surface": body.surface, "state": "answered",
            })
        finally:
            if inflight.get(decision_id) is mine:
                del inflight[decision_id]
            mine.set_result(None)
        updated = await asyncio.to_thread(store.get, decision_id)
        # Ours landed if the record is answered with our answer — or with the exact text we sent,
        # which is what the turn driver records when no resolution metadata reaches it.
        accepted = updated.state == "answered" and updated.answer in (answer, text)
        # Closed by something other than this answer (another surface, expiry, a cancel): ours
        # lost, whatever the transport said. A still-pending record is failed delivery: retryable.
        if not updated.pending and not accepted:
            raise HTTPException(status_code=409, detail=updated.to_dict())
        # The checkpoint is authoritative even if the HTTP response was lost or timed out.
        return {"decision": updated.to_dict(), "resumed": updated.state == "answered"}

    @app.post("/api/decisions/{decision_id}/cancel")
    async def _api_cancel_decision(decision_id: str, body: CancelRequest | None = None):
        """Cancel a pending decision (the operator dismissed it) and unblock the parked turn.

        404 for an unknown id, 409 when it is no longer pending. The record is marked
        withdrawn (recording the operator's intent + publishing ``decision.changed``), then
        the parked checkpoint interrupt is CLEARED by resuming the turn with a cancellation
        sentinel — so the session is NOT left blocked for later autonomous (scheduler /
        inbox / webhook) turns. Clearing is best-effort: the autonomous hold also refuses to
        park on a non-pending decision, so a failed clear can never wedge the session."""
        try:
            updated = await asyncio.to_thread(decision_store().withdraw, decision_id, body.reason if body else "")
        except DecisionNotFound as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except DecisionClosed as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        await _resume_parked_turn(updated.session_id, CANCELLED_RESUME)
        return {"decision": updated.to_dict()}
