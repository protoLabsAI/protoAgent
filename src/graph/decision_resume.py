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


async def resume_parked_turn(session_id: str, answer: str, decision_id: str | None = None, *, resolution: dict | None = None) -> bool:
    """Resume the turn parked on this decision with ``answer``, over the loopback A2A
    endpoint — the SAME HITL-resume entry point the console uses.

    With a ``decision_id`` (the answer path) the id is checked against the checkpoint
    interrupt under the turn lock, and the turn driver closes the record only after the
    checkpoint consumes the answer. WITHOUT one (the cancel path, which has already closed
    the record) the check is skipped — the resume only has to CLEAR the lingering checkpoint
    interrupt so the session isn't wedged for later autonomous turns. Delivery failure
    leaves the interrupt in place and returns False so callers can report failed delivery.
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

