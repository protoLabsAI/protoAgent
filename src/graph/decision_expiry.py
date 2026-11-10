"""Retryable decision expiry, driven by an independent host maintenance cadence."""

from datetime import datetime
import logging

from graph.decisions import DecisionStore

log = logging.getLogger("protoagent.decisions")

# Maximum polling delay on a healthy, responsive host (delivery time is additional).
# Independent of A2A retention/reaping; also bounds discovery of newly created rows.
DECISION_EXPIRY_INTERVAL_S = 1.0


async def sweep_expired_decisions(store: DecisionStore, resume) -> int:
    """Deliver stated defaults through the guarded resume path; close only on consumption.

    No expiry means indefinite waiting. Failed delivery stays pending for the next tick.
    The resume callback validates the decision/checkpoint under the session lock, so an
    operator answer racing the sweep wins without a second graph invocation.
    """
    now = store._clock()
    count = 0
    for rec in store.list(state="pending", limit=500):
        if not rec.expires_at or not rec.default_option or datetime.fromisoformat(rec.expires_at) > now:
            continue
        text = f"No answer before {rec.expires_at}; applying the stated default: {rec.option_label(rec.default_option)} ({rec.default_option})"
        try:
            await resume(rec.session_id, text, rec.id, resolution={"state": "expired", "answer": rec.default_option})
            count += store.get(rec.id).state == "expired"
        except Exception:
            log.exception("[decision] expiry delivery failed for %s", rec.id)
    return count
