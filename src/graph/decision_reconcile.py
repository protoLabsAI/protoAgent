"""Close decision records stranded without a parked interrupt (#5107).

A ``pending`` record is only answerable while its session checkpoint still holds the
interrupt it was raised on. When that interrupt is gone — the answer was consumed but the
process died before the record closed, a crash, a bug — the record can never be answered:
every answer is refused under the turn lock (``DecisionClosed``) and the console would list it
forever. This module is the one place that decides "provably stranded" and closes such a
record as ``withdrawn`` with a human reason, so the card says it was already acted on.

Used by the answer guard (``server.turn_control.validate_decision_resume``, under the session
lock), by the boot reconcile (``reconcile_pending_decisions``) and by the cancel route (to skip
a pointless resume). ``graph`` must never import ``server`` or ``operator_api``: callers pass
the compiled graph and, where they have one, the per-thread lock factory.
"""

from __future__ import annotations

import contextlib
import logging

from graph.decisions import Decision, DecisionClosed, DecisionNotFound, DecisionStore

log = logging.getLogger("protoagent.decisions")

#: Shown after "{Agent} doesn't need this any more." on the card (docs/design/decision-needed.md §4.4).
STRANDED_REASON = "It had already moved past this question, so there's nothing left to answer."


async def interrupt_parked(graph, rec: Decision) -> bool | None:
    """Whether ``rec``'s interrupt is still pending on its thread's checkpoint.

    ``True``: still parked (answerable). ``False``: the checkpoint was read and provably no
    longer holds it. ``None``: unknown — no graph, a record without an interrupt id, or the
    checkpoint could not be read. Only ``False`` may ever close a record.

    Reads the snapshot exactly as the turn drivers do (``server.chat._pending_interrupt``):
    task-level interrupts of tasks without a result, falling back to snapshot-level ones."""
    if graph is None or not rec.interrupt_id or not rec.thread_id:
        return None
    try:
        snapshot = await graph.aget_state({"configurable": {"thread_id": rec.thread_id}})
    except Exception:  # noqa: BLE001 — unknown, never a reason to close
        log.debug("[decision] could not read checkpoint for %s", rec.id, exc_info=True)
        return None
    tasks = getattr(snapshot, "tasks", ()) or ()
    pending = {
        getattr(i, "id", None)
        for t in tasks
        if getattr(t, "result", None) is None
        for i in (getattr(t, "interrupts", ()) or ())
    }
    if not tasks:
        pending.update(getattr(i, "id", None) for i in (getattr(snapshot, "interrupts", ()) or ()))
    return rec.interrupt_id in pending


def close_stranded(store: DecisionStore, rec: Decision) -> Decision | None:
    """Withdraw a stranded record with :data:`STRANDED_REASON` (publishes ``decision.changed``).

    Returns the record as it now stands — already-closed records are returned unchanged, so a
    racing answer or cancel keeps its own outcome."""
    try:
        closed = store.withdraw(rec.id, STRANDED_REASON)
    except DecisionClosed:
        return store.get(rec.id)
    except DecisionNotFound:
        return None
    log.info("[decision] %s no longer owns an interrupt on %s; closed it as withdrawn", rec.id, rec.thread_id)
    return closed


async def reconcile_pending_decisions(store: DecisionStore, graph, lock_for=None) -> int:
    """Boot reconcile: close every pending record whose checkpoint provably lost its interrupt.

    ``lock_for(thread_id)`` (the server's per-thread turn lock) serializes each check with any
    turn already running on that thread, so a decision being raised right now is never read
    half-written. A record whose interrupt is still parked — or whose state can't be read —
    is left alone. Returns the number closed."""
    closed = 0
    for rec in store.list(state="pending", limit=500):
        lock = lock_for(rec.thread_id) if lock_for and rec.thread_id else contextlib.nullcontext()
        async with lock:
            current = store.get(rec.id)
            if current is None or not current.pending:
                continue
            if await interrupt_parked(graph, current) is False and close_stranded(store, current) is not None:
                closed += 1
    if closed:
        log.info("[decision] boot reconcile closed %d stranded decision(s)", closed)
    return closed
