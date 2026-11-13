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

from fastapi import Request
from pydantic import BaseModel

from graph.decision_resume import CANCELLED_RESUME as CANCELLED_RESUME
from graph.decision_resume import resume_parked_turn as _resume_parked_turn

log = logging.getLogger("protoagent.decisions")

#: How long an answer request waits for the resumed run to CONSUME the answer (the parked
#: checkpoint interrupt resolves) before replying 202 "on its way" (#5099). Consumption is
#: normally milliseconds; this only runs out when the session is busy with another turn.
#: Delivery continues in the background either way and keeps its claim, so a retry joins it
#: instead of sending the answer again. Read at call time (tests shrink it).
ACCEPT_WAIT_S = 10.0


class AnswerRequest(BaseModel):
    """The operator's answer: a defined option by id (validated against the record's options)
    or a free-text answer. At least one is required."""

    option_id: str | None = None
    text: str | None = None
    surface: str = "console"


class CancelRequest(BaseModel):
    reason: str = ""


async def _restart_recovery_action(rec, ref: dict, *, answer: str, answered_by: str, answered_surface: str):
    """Deliver a restart-recovery decision's answer (ADR 0129 D7 S7): Resume / Re-run /
    Drop on the occurrence a restart left ``interrupted_needs_operator``. There is no
    parked checkpoint interrupt to resume — the server-owned handler performs the action
    (under the execution session's turn lock where it touches the thread) and closes the
    ledger itself with a CAS, so of two concurrent answers exactly one lands and the
    loser surfaces as this route's 409. Reached through ``STATE.interrupted_recovery``:
    this module must not import ``server``."""
    from fastapi import HTTPException

    from graph.decisions import DecisionClosed, DecisionError
    from runtime.state import STATE

    recovery = getattr(STATE, "interrupted_recovery", None)
    if recovery is None:
        # Keep the record pending (retryable) rather than guess: a degraded boot has no
        # checkpoint/task seam to act through.
        raise HTTPException(status_code=503, detail="restart recovery is unavailable on this server")
    try:
        await recovery.operator_action(
            rec, ref, answer=answer, answered_by=answered_by, answered_surface=answered_surface
        )
    except DecisionClosed as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except DecisionError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


def _resolve_answer(rec, body: AnswerRequest) -> str:
    """The answer string to resume the turn with, validated against the record's options.

    An ``option_id`` must name one of the decision's options (matched by id, then by label)
    and resolves to that option's canonical id; a free-text ``text`` is taken verbatim. Raises
    :class:`fastapi.HTTPException` 400 on an unknown option or when neither field is given."""
    from fastapi import HTTPException

    option_id = (body.option_id or "").strip()
    text = (body.text or "").strip()
    if body.option_id is not None and not option_id:
        # A blank option id is a malformed request, not a free-text answer in disguise.
        raise HTTPException(status_code=400, detail="option_id is blank")
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
        restart_recovery_ref,
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

    # Answers being delivered right now, by decision id. Two surfaces answering the same
    # decision at once both used to send a resume: the loser's resume was refused under the
    # turn lock, but only after its task and chat message existed, and the route still said
    # 200 "Sent" (release acceptance bd-nfd6g.17, D3). A second answer now waits for the one in
    # flight to settle instead of racing it. A claim is held until the answer is consumed or
    # its delivery ends — never released by a timeout while the resume is still queued (#5099).
    # Single-process state: one server owns the ledger (a restart drops the claim along with
    # the turn that held it, so the decision is answerable again).
    inflight: dict[str, asyncio.Future] = {}
    # Dismissals being written right now, by decision id. A cancel holds this across its
    # check-and-withdraw (which hops to a thread), so an answer arriving in that window waits
    # and then sees the record closed, instead of delivering to a decision being withdrawn.
    cancelling: dict[str, asyncio.Future] = {}
    deliveries: set[asyncio.Task] = set()

    def delivering(decision_id: str) -> bool:
        claim = inflight.get(decision_id)
        return claim is not None and not claim.done()

    def with_delivery(rec) -> dict:
        # Every single-record response carries it, so a 409 detail equals a fresh GET.
        return {**rec.to_dict(), "delivering": rec.pending and delivering(rec.id)}

    def delivery_done(task):
        deliveries.discard(task)
        if task.cancelled():
            return
        exc = task.exception()  # Retrieve errors even after the request has returned.
        if exc is not None and not isinstance(exc, HTTPException):
            log.error("[decision] answer delivery crashed", exc_info=exc)

    def on_its_way(rec):
        # Still pending, but an answer is being delivered: say so honestly instead of either
        # "sent" (not consumed yet) or "failed" (it may well land). 202 Accepted.
        from fastapi.responses import JSONResponse

        return JSONResponse(status_code=202, content={"decision": with_delivery(rec), "resumed": False, "delivering": True})

    @app.get("/api/decisions/{decision_id}")
    async def _api_decision(decision_id: str):
        """One decision by id, or 404. ``delivering`` is true while an answer to it is on its
        way to the agent (accepted, not yet consumed) — the console's "delivery unknown" check."""
        rec = await asyncio.to_thread(decision_store().get, decision_id)
        if rec is None:
            raise HTTPException(status_code=404, detail=f"no decision {decision_id!r}")
        return with_delivery(rec)

    @app.post("/api/decisions/{decision_id}/answer")
    async def _api_answer_decision(decision_id: str, body: AnswerRequest, request: Request):
        """Answer a pending decision and resume its parked turn.

        Validates the answer against the record's options (400 on an unknown option or an
        empty body). Closed decisions return 409 with the record, unknown ids 404. Replies as
        soon as the resumed run consumes the answer (200, ``resumed: true``) — the rest of the
        turn runs in the background and its outcome is logged. A delivery that fails before
        consumption leaves the decision pending: 200 ``resumed: false``, retryable. A delivery
        still queued after :data:`ACCEPT_WAIT_S` (the session is busy) is 202
        ``delivering: true``; answering again meanwhile joins it rather than sending twice."""
        store = decision_store()
        rec = await asyncio.to_thread(store.get, decision_id)
        if rec is None:
            raise HTTPException(status_code=404, detail=f"no decision {decision_id!r}")
        if not rec.pending:
            raise HTTPException(status_code=409, detail=with_delivery(rec))
        answer = _resolve_answer(rec, body)
        loop = asyncio.get_running_loop()
        deadline = loop.time() + ACCEPT_WAIT_S
        while (other := inflight.get(decision_id) or cancelling.get(decision_id)) is not None:
            # Another answer is mid-delivery. If it closes the decision, ours lost: 409 with the
            # real record. If it fails delivery (still pending), take our turn to deliver. If it
            # is still queued, it is on its way: report that, never start a second turn.
            remaining = deadline - loop.time()
            if remaining <= 0:
                return on_its_way(rec)
            try:
                await asyncio.wait_for(asyncio.shield(other), timeout=min(remaining, 0.25))
            except asyncio.TimeoutError:
                pass
            rec = await asyncio.to_thread(store.get, decision_id)
            if rec is None or not rec.pending:
                raise HTTPException(status_code=409, detail=with_delivery(rec) if rec else None)
        mine = loop.create_future()
        inflight[decision_id] = mine
        # _resolve_answer rejects a blank option_id, so a non-None one here names an option.
        text = f"Operator chose: {rec.option_label(answer)} ({answer})" if body.option_id is not None else answer
        actor = getattr(request.state, "device_id", None) or "operator"

        async def deliver_answer():
            try:
                current = await asyncio.to_thread(store.get, decision_id)
                if current is None or not current.pending:
                    raise HTTPException(status_code=409, detail=with_delivery(current) if current else None)
                # Its result isn't needed: the ledger, re-read below, says whose answer landed.
                recovery_ref = restart_recovery_ref(current)
                if recovery_ref is not None:
                    # ADR 0129 D7 S7: no parked interrupt — the answer performs
                    # Resume / Re-run / Drop on the held occurrence instead.
                    delivery = asyncio.create_task(_restart_recovery_action(
                        current, recovery_ref, answer=answer,
                        answered_by=actor, answered_surface=body.surface,
                    ))
                else:
                    delivery = asyncio.create_task(_resume_parked_turn(rec.session_id, text, decision_id, resolution={
                        "answer": answer, "answered_by": actor,
                        "answered_surface": body.surface, "state": "answered",
                    }))
                deliveries.add(delivery)
                delivery.add_done_callback(delivery_done)
                # Keep delivery alive while returning as soon as consumption is durable. Poll
                # fast while consumption is imminent, then back off for a resume queued behind
                # a busy session (it can wait minutes for the lock).
                poll_s = 0.02
                while not delivery.done():
                    current = await asyncio.to_thread(store.get, decision_id)
                    if current is not None and not current.pending:
                        break
                    await asyncio.wait({delivery}, timeout=poll_s)
                    poll_s = min(poll_s * 1.5, 0.5)
                if delivery.done():
                    await delivery
            finally:
                if inflight.get(decision_id) is mine:
                    del inflight[decision_id]
                mine.set_result(None)
        # A disconnected request must not release arbitration while delivery is live.
        accepted_delivery = asyncio.create_task(deliver_answer())
        deliveries.add(accepted_delivery)
        accepted_delivery.add_done_callback(delivery_done)
        # asyncio.wait never cancels it: a disconnect or the deadline leaves delivery running.
        await asyncio.wait({accepted_delivery}, timeout=max(0.0, deadline - loop.time()))
        if not accepted_delivery.done():
            current = await asyncio.to_thread(store.get, decision_id)
            if current is not None and current.pending:
                return on_its_way(current)
            await accepted_delivery  # consumed at the deadline; settles at once
        elif (exc := accepted_delivery.exception()) is not None:
            raise exc
        updated = await asyncio.to_thread(store.get, decision_id)
        if updated is None:
            raise HTTPException(status_code=404, detail=f"no decision {decision_id!r}")
        # Ours landed if the record is answered with our answer — or with the exact text we sent,
        # which is what the turn driver records when no resolution metadata reaches it.
        accepted = updated.state == "answered" and updated.answer in (answer, text)
        # Closed by something other than this answer (another surface, expiry, a cancel): ours
        # lost, whatever the transport said. A still-pending record is failed delivery: retryable.
        if not updated.pending and not accepted:
            raise HTTPException(status_code=409, detail=with_delivery(updated))
        # The checkpoint is authoritative even if the HTTP response was lost or timed out.
        return {"decision": with_delivery(updated), "resumed": updated.state == "answered"}

    @app.post("/api/decisions/{decision_id}/cancel")
    async def _api_cancel_decision(decision_id: str, body: CancelRequest | None = None):
        """Cancel a pending decision (the operator dismissed it) and unblock the parked turn.

        404 for an unknown id, 409 when it is no longer pending. The record is marked
        withdrawn (recording the operator's intent + publishing ``decision.changed``), then
        the parked checkpoint interrupt is CLEARED by resuming the turn with a cancellation
        sentinel — so the session is NOT left blocked for later autonomous (scheduler /
        inbox / webhook) turns. Clearing is best-effort: the autonomous hold also refuses to
        park on a non-pending decision, so a failed clear can never wedge the session.

        Returns as soon as the record is withdrawn (#5107): the clearing resume runs in the
        background, so Dismiss never waits on the agent's turn (the #5099 freeze). A record
        whose checkpoint provably no longer holds its interrupt (stranded) gets no resume at
        all — there is nothing to clear, and a resume would run as a fresh turn."""
        from graph.decision_reconcile import interrupt_parked
        from runtime.state import STATE

        store = decision_store()
        rec = await asyncio.to_thread(store.get, decision_id)
        if rec is None:
            raise HTTPException(status_code=404, detail=f"no decision {decision_id!r}")
        # A restart-recovery decision (ADR 0129 D7 S7) has no checkpoint interrupt to
        # clear: withdrawing the record IS the whole dismissal — a clearing resume would
        # instead start a fresh turn on the job session, which only Re-run may do. The
        # held occurrence stays parked, visible in the job's diagnostics.
        parked = False if restart_recovery_ref(rec) is not None else await interrupt_parked(
            getattr(STATE, "graph", None), rec
        )
        # No await from here to the claim: the check-and-claim is atomic with respect to the
        # answer route's claim (both run on this event loop), and the claim is held across the
        # threaded withdraw (#5112 review).
        if not rec.pending or delivering(decision_id) or decision_id in cancelling:
            # Closed already, an answer is mid-delivery (withdrawing would pull the record out
            # from under a resume that may be consuming it), or another dismissal is mid-write.
            raise HTTPException(status_code=409, detail=with_delivery(rec))
        claim = asyncio.get_running_loop().create_future()
        cancelling[decision_id] = claim
        try:
            updated = await asyncio.to_thread(store.withdraw, decision_id, body.reason if body else "")
        except DecisionNotFound as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except DecisionClosed as exc:
            current = await asyncio.to_thread(store.get, decision_id)
            raise HTTPException(status_code=409, detail=with_delivery(current) if current else str(exc)) from exc
        finally:
            if cancelling.get(decision_id) is claim:
                del cancelling[decision_id]
            claim.set_result(None)
        if parked is not False:
            clearing = asyncio.create_task(_resume_parked_turn(updated.session_id, CANCELLED_RESUME))
            deliveries.add(clearing)
            clearing.add_done_callback(delivery_done)
        return {"decision": updated.to_dict()}
