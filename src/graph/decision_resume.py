"""Shared loopback delivery for decision answers and dismissals."""

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
) -> bool:
    """Resume the turn parked on this decision with ``answer``, over the loopback A2A
    endpoint — the SAME HITL-resume entry point the console uses.

    With a ``decision_id`` (the answer path) the id is checked against the checkpoint
    interrupt under the turn lock, and the turn driver closes the record only after the
    checkpoint consumes the answer. WITHOUT one (the cancel path, which has already closed
    the record) the check is skipped — the resume only has to CLEAR the lingering checkpoint
    interrupt so the session isn't wedged for later autonomous turns. Delivery failure
    leaves the interrupt in place and returns False so callers can report failed delivery.

    ``wait_for_turn`` drops the read deadline: the call returns when the resumed turn ends,
    however long that is. It defaults on for an operator answer (a ``resolution`` in state
    ``answered``, which only the answer route sends) and off for dismissals and expiry
    defaults. Either way a turn that ends failed is a WARNING naming the decision, never
    silent — including one that fails after consuming the answer. True only when the turn
    completed (or paused again) without failing.
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
        if resolution:
            metadata["decision_resolution"] = resolution
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
    if wait_for_turn is None:
        wait_for_turn = bool(decision_id and resolution and resolution.get("state") == "answered")
    timeout = httpx.Timeout(RESUME_TIMEOUT_S, read=None) if wait_for_turn else RESUME_TIMEOUT_S
    label = f"decision {decision_id}" if decision_id else "dismissal"
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            r = await client.post(f"http://127.0.0.1:{port}/a2a", headers=headers, json=body)
        if r.status_code >= 400:
            log.warning("[decision] resume for %s failed: HTTP %d %s", session_id, r.status_code, r.text[:200])
            return False
        payload = r.json()
        if payload.get("error"):
            log.warning("[decision] resume for %s (%s) refused: %s", session_id, label, str(payload["error"])[:200])
            return False
        state, text = _task_outcome(payload)
        if state in _FAILED_TASK_STATES:
            log.warning("[decision] resumed turn for %s (%s) ended %s: %s", session_id, label, state, text[:300])
            return False
        if wait_for_turn:
            log.info("[decision] resumed turn for %s (%s) finished: %s", session_id, label, state or "ok")
        return True
    except httpx.TimeoutException:
        # The turn may still be running server-side: delivery is unknown, not refused.
        log.warning("[decision] resume for %s (%s) timed out after %ss; the turn may still be running",
                    session_id, label, RESUME_TIMEOUT_S)
        return False
    except Exception:  # noqa: BLE001 — a resume transport error must not 500 the answer route
        log.exception("[decision] resume POST failed for %s", session_id)
        return False
