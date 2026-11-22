"""Retryable decision expiry, driven by an independent host maintenance cadence."""

from datetime import datetime
import logging

from graph.decisions import DecisionError, DecisionStore, is_unpaused_decision

log = logging.getLogger("protoagent.decisions")

# Maximum polling delay on a healthy, responsive host (delivery time is additional).
# Independent of A2A retention/reaping; also bounds discovery of newly created rows.
DECISION_EXPIRY_INTERVAL_S = 1.0


async def sweep_expired_decisions(store: DecisionStore, resume, deliver=None) -> int:
    """Deliver stated defaults through the guarded resume path; close only on consumption.

    No expiry means indefinite waiting. Failed delivery stays pending for the next tick.
    The resume callback validates the decision/checkpoint under the session lock, so an
    operator answer racing the sweep wins without a second graph invocation.

    A no-pause decision (#5678 slice 3a) has no parked turn to resume. It closes in the
    ledger first — as ``expired`` with its default as the answer, or with no answer when it
    has no default — and only a recorded default is then delivered as a new turn through
    ``deliver(decision_id)`` (default :func:`graph.decision_resume.deliver_decision_answer`,
    the answer route's path). A failed delivery keeps the applied default saved, exactly like
    an operator answer whose delivery failed.
    """
    now = store._clock()
    count = 0
    for rec in store.list(state="pending", limit=500):
        if is_unpaused_decision(rec):
            if rec.expires_at and datetime.fromisoformat(rec.expires_at) <= now:
                count += await _expire_unpaused(store, rec, deliver)
            continue
        if not rec.expires_at or not rec.default_option or datetime.fromisoformat(rec.expires_at) > now:
            continue
        text = f"No answer before {rec.expires_at}; applying the stated default: {rec.option_label(rec.default_option)} ({rec.default_option})"
        try:
            await resume(rec.session_id, text, rec.id, resolution={"state": "expired", "answer": rec.default_option})
            count += store.get(rec.id).state == "expired"
        except Exception:
            log.exception("[decision] expiry delivery failed for %s", rec.id)
    return count


async def _expire_unpaused(store: DecisionStore, rec, deliver) -> int:
    """Close one due no-pause decision, then deliver its default (if any) as a turn."""
    try:
        closed = store.expire(rec.id)
    except DecisionError:
        # Answered, dismissed or withdrawn meanwhile (DecisionClosed), or gone: theirs stands.
        return 0
    if closed.answer is None:
        log.info("[decision] %s expired with no default; no turn started", rec.id)
        return 1
    if deliver is None:
        from graph.decision_resume import deliver_decision_answer as deliver
    try:
        await deliver(rec.id)
    except Exception:
        log.exception("[decision] expiry default delivery failed for %s", rec.id)
    return 1
