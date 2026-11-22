"""Shared loopback delivery for decision answers and dismissals."""

import enum
import logging

log = logging.getLogger("protoagent.decisions")

#: What ``request_decision`` returns to the model when the operator DISMISSES a decision
#: instead of choosing an option. The cancel route resumes the parked turn with this so the
#: checkpoint interrupt is cleared (the turn stops waiting); the ledger record is already
#: marked ``withdrawn``, so the resume only unblocks the thread.
CANCELLED_RESUME = (
    "[decision dismissed by the operator] The operator cancelled this decision without "
    "choosing an option. Do not take the action you were deciding on — acknowledge that it "
    "was cancelled and end your turn."
)


#: Transport deadline for a resume whose caller waits for the reply (dismissals, the expiry
#: sweep). An operator ANSWER does not use it: the answer route acknowledges on checkpoint
#: consumption, so its loopback call carries no read deadline and ends when the resumed turn
#: ends (#5099) — a turn longer than this is not a failed delivery.
RESUME_TIMEOUT_S = 120.0

_FAILED_TASK_STATES = frozenset({"TASK_STATE_FAILED", "TASK_STATE_CANCELED", "TASK_STATE_REJECTED",
                                 "failed", "canceled", "rejected"})


#: Request-metadata key carrying the single-operation loopback grant a decision resume rides.
#: On a fleet member the self-invocation credential IS the fleet service token (a peer kind,
#: ``auth._classify_token``), so the bearer alone cannot tell this server's own answer / cancel
#: / expiry delivery from a sister agent's message. The grant is a loopback delegation (ADR 0131
#: P1a.2, ``graph.fleet.delegation``) minted with this process's own key and bound to the
#: session and decision: only code in this process can mint it, and the executor redeems it
#: (``a2a_impl.executor``) before the turn may answer the paused decision (#5678 slice 5b).
RESUME_GRANT_KEY = "decision_resume_grant"
RESUME_GRANT_AUDIENCE = "decision-resume"


def resume_grant_operation(decision_id: str | None) -> str:
    """The operation a resume grant is bound to: the decision it answers, or a dismissal."""
    return f"decision:{decision_id}" if decision_id else "decision:dismiss"


def mint_resume_grant(session_id: str, decision_id: str | None) -> str:
    """Mint the loopback grant for one decision resume; "" when the key is unavailable.

    Best-effort: without a grant the resume still carries the instance's own credential, which
    is the human-operator kind on a standalone instance (only a fleet member needs the grant)."""
    try:
        from graph.fleet.delegation import ISSUER_LOOPBACK, mint_delegation

        return mint_delegation(
            audience=RESUME_GRANT_AUDIENCE,
            method="SendMessage",
            context=session_id,
            operation=resume_grant_operation(decision_id),
            issuer=ISSUER_LOOPBACK,
        )
    except Exception:  # noqa: BLE001 — a missing grant degrades to the bearer, never a crash
        log.warning("[decision] could not mint a resume grant for %s", session_id, exc_info=True)
        return ""


class ResumeOutcome(enum.Enum):
    """What a loopback resume achieved. Truthy when the resume reached the parked turn.

    - ``DELIVERED``: the resumed turn completed (or paused again).
    - ``TURN_FAILED``: the resume reached the agent, but its turn ended failed/cancelled/
      rejected. For a dismissal the interrupt was still taken; for an answer, the ledger
      (closed on consumption) says whether the answer landed.
    - ``NOT_DELIVERED``: refused or never sent (no port, HTTP error, JSON-RPC error,
      transport error). The interrupt is still parked.
    - ``UNKNOWN``: the transport timed out; the turn may still be running.
    """

    DELIVERED = "delivered"
    TURN_FAILED = "turn_failed"
    NOT_DELIVERED = "not_delivered"
    UNKNOWN = "unknown"

    def __bool__(self) -> bool:
        return self in (ResumeOutcome.DELIVERED, ResumeOutcome.TURN_FAILED)


def _task_outcome(payload: dict) -> tuple[str, str]:
    """``(state, first status text)`` from a SendMessage reply; empty strings when absent."""
    def obj(value) -> dict:
        return value if isinstance(value, dict) else {}

    result = obj(obj(payload).get("result"))
    status = obj(obj(result.get("task") or result).get("status"))
    parts = obj(status.get("message")).get("parts") or []
    text = next((str(p["text"]) for p in parts if isinstance(p, dict) and p.get("text")), "")
    return str(status.get("state") or ""), text


async def resume_parked_turn(
    session_id: str,
    answer: str,
    decision_id: str | None = None,
    *,
    resolution: dict | None = None,
    wait_for_turn: bool | None = None,
) -> ResumeOutcome:
    """Resume the turn parked on this decision with ``answer``, over the loopback A2A
    endpoint — the SAME HITL-resume entry point the console uses.

    With a ``decision_id`` (the answer path) the id is checked against the checkpoint
    interrupt under the turn lock, and the turn driver closes the record only after the
    checkpoint consumes the answer. WITHOUT one (the cancel path, which has already closed
    the record) the check is skipped — the resume only has to CLEAR the lingering checkpoint
    interrupt so the session isn't wedged for later autonomous turns. Delivery failure
    leaves the interrupt in place and returns a falsy :class:`ResumeOutcome` so callers can
    report failed delivery.

    ``wait_for_turn`` drops the read deadline: the call returns when the resumed turn ends,
    however long that is. It defaults on for an operator answer (a ``resolution`` in state
    ``answered``, which only the answer route sends) and off for dismissals and expiry
    defaults. Either way a turn that ends failed is a WARNING naming the decision, never
    silent — including one that fails after consuming the answer (``TURN_FAILED``).
    """
    from runtime.state import STATE

    port = getattr(STATE, "active_port", None)
    if not port:
        log.warning("[decision] no active port — cannot resume parked turn for %s", session_id)
        return ResumeOutcome.NOT_DELIVERED
    # The operator is answering/dismissing: a live human, so an empty origin (interactive) —
    # `hitl_resume` converts this to a graph resume rather than a fresh turn. A `decision_id`
    # is carried only on the answer path (it gates validate_decision_resume); the cancel path
    # omits it so the resume just clears the interrupt on the already-closed record.
    metadata = {"hitl_resume": True}
    if grant := mint_resume_grant(session_id, decision_id):
        metadata[RESUME_GRANT_KEY] = grant
    if decision_id:
        metadata["decision_id"] = decision_id
        if resolution:
            metadata["decision_resolution"] = resolution
    if wait_for_turn is None:
        wait_for_turn = bool(decision_id and resolution and resolution.get("state") == "answered")
    label = f"decision {decision_id}" if decision_id else "dismissal"
    outcome, state = await _post_loopback(port, session_id, answer, metadata, label=label,
                                          wait_for_turn=wait_for_turn)
    if outcome is ResumeOutcome.DELIVERED and wait_for_turn:
        log.info("[decision] resumed turn for %s (%s) finished: %s", session_id, label, state or "ok")
    return outcome


async def _post_loopback(
    port, session_id: str, text: str, metadata: dict, *, label: str,
    wait_for_turn: bool = False, configuration: dict | None = None,
) -> tuple[ResumeOutcome, str]:
    """POST one SendMessage to this server's own ``/a2a`` and classify the reply.

    Shared by the parked-turn resume and the no-pause new-turn delivery: same envelope,
    self-invocation headers, and outcome mapping. Returns ``(outcome, task state)``;
    ``DELIVERED`` covers any non-failed reply, including one with no task state."""
    import uuid

    import httpx

    from a2a_impl.auth import self_invocation_headers

    headers = {"Content-Type": "application/json", "A2A-Version": "1.0"}
    headers.update(self_invocation_headers())
    message_id = str(uuid.uuid4())
    params: dict = {
        "message": {
            "role": "ROLE_USER",
            "parts": [{"text": text}],
            "messageId": message_id,
            "contextId": session_id,
            "metadata": metadata,
        },
    }
    if configuration:
        params = {"configuration": configuration, **params}
    body = {"jsonrpc": "2.0", "id": message_id, "method": "SendMessage", "params": params}
    timeout = httpx.Timeout(RESUME_TIMEOUT_S, read=None) if wait_for_turn else RESUME_TIMEOUT_S
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            r = await client.post(f"http://127.0.0.1:{port}/a2a", headers=headers, json=body)
        if r.status_code >= 400:
            log.warning("[decision] resume for %s failed: HTTP %d %s", session_id, r.status_code, r.text[:200])
            return ResumeOutcome.NOT_DELIVERED, ""
        payload = r.json()
        if payload.get("error"):
            log.warning("[decision] resume for %s (%s) refused: %s", session_id, label, str(payload["error"])[:200])
            return ResumeOutcome.NOT_DELIVERED, ""
        state, reply = _task_outcome(payload)
        if state in _FAILED_TASK_STATES:
            log.warning("[decision] resumed turn for %s (%s) ended %s: %s", session_id, label, state, reply[:300])
            return ResumeOutcome.TURN_FAILED, state
        return ResumeOutcome.DELIVERED, state
    except httpx.TimeoutException:
        # The turn may still be running server-side: delivery is unknown, not refused.
        log.warning("[decision] resume for %s (%s) timed out after %ss; the turn may still be running",
                    session_id, label, RESUME_TIMEOUT_S)
        return ResumeOutcome.UNKNOWN, ""
    except Exception:  # noqa: BLE001 — a resume transport error must not 500 the answer route
        log.exception("[decision] resume POST failed for %s", session_id)
        return ResumeOutcome.NOT_DELIVERED, ""


def decision_answer_message(decision_id: str, session_id: str) -> str:
    """Read a no-pause answer from the ledger, never from delivery message text.

    Chat drivers can use this reader under their turn lock. Bind the reference to
    its session so another thread cannot obtain the answer.
    """
    from graph.decisions import DecisionError, decision_store, is_unpaused_decision

    rec = decision_store().get(decision_id)
    if (rec is None or rec.session_id != session_id or not is_unpaused_decision(rec)
            or rec.state != "answered"):
        raise DecisionError("decision is not an answered no-pause decision on this session")
    answer = (f"Operator chose: {rec.option_label(rec.answer)} ({rec.answer})"
              if rec.answer_kind == "option" else rec.answer)
    return f"The operator answered your decision.\nQuestion: {rec.question}\nAnswer: {answer}"


async def deliver_decision_answer(decision_id: str) -> ResumeOutcome:
    """Start a new turn for a saved answer, stamping delivery only on acceptance.

    Decision metadata carries only its id. Receivers use ``decision_answer_message``
    instead of trusting message text. Failed delivery preserves the saved answer for
    next-turn recovery. The answer route's claim and ledger CAS arbitrate submissions.
    """
    import asyncio

    from graph.decisions import decision_store, is_unpaused_decision
    from runtime.state import STATE

    store = decision_store()
    rec = await asyncio.to_thread(store.get, decision_id)
    if rec is None or not is_unpaused_decision(rec) or rec.state != "answered":
        return ResumeOutcome.NOT_DELIVERED
    if rec.delivered_at:
        return ResumeOutcome.DELIVERED
    port = getattr(STATE, "active_port", None)
    if not port:
        log.warning("[decision] no active port — cannot deliver answer for %s", decision_id)
        return ResumeOutcome.NOT_DELIVERED
    text = await asyncio.to_thread(decision_answer_message, decision_id, rec.session_id)
    metadata = {"decision_id": decision_id}
    if grant := mint_resume_grant(rec.session_id, decision_id):
        metadata[RESUME_GRANT_KEY] = grant
    outcome, state = await _post_loopback(port, rec.session_id, text, metadata,
                                          label=f"decision {decision_id}",
                                          configuration={"returnImmediately": True})
    if outcome is not ResumeOutcome.DELIVERED:
        return outcome
    if not state:
        # No task came back, so nothing proves a turn was started: keep it undelivered.
        return ResumeOutcome.NOT_DELIVERED
    await asyncio.to_thread(store.mark_delivered, decision_id)
    return ResumeOutcome.DELIVERED
