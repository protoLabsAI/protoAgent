"""Follow-up backoff registry (ADR 0129 S14, #5270).

A recurring follow-up whose origin chat is *waiting on the operator* and that keeps
reporting nothing new backs off to one fire per hour instead of re-reporting the same
parked work every slot. Two subsystems share this in-memory registry:

- the server's scheduled-card path (``server.a2a``) counts the unchanged runs, applies
  the waiting test, and ARMS the backoff (``set_backoff``) when a quiet follow-up's chat
  is waiting;
- the local scheduler (``scheduler.local``) READS it (``is_backed_off``) to skip a due
  occurrence while the backoff holds — without claiming it, firing it, counting a
  failure or touching the stored schedule.

The scheduler also RE-TESTS, each tick, whether the origin chat is still waiting, so a
backoff clears the moment the operator answers the waiting task or settles the decision
instead of stalling for the whole hour. The scheduler owns neither the tasks nor the
decision store (import-layering contract), so the server — which does — injects that
predicate here via ``set_waiting_check``; ``still_waiting`` runs it.

It lives in ``scheduler`` (not ``server``) because the scheduler must never import the
server bootstrap (import-layering contract). Keyed by ``(job_id, generation)`` — the
generation is the job's ``created_at``, so a cancel-and-recreate on the same id starts
clean (its new generation has no entry). Purely in memory: a restart clears every
backoff, so at worst one extra card posts after a restart, never a missed resume.
"""

from __future__ import annotations

import threading
from collections.abc import Callable

_lock = threading.Lock()

# (job_id, generation) -> POSIX timestamp (UTC epoch seconds) until which the recurring
# job's due occurrences are skipped. Set by the server when a quiet follow-up whose chat
# is waiting hits the unchanged-run threshold; read by the scheduler each tick.
_BACKOFF: dict[tuple[str, str], float] = {}

# The server-injected "is this origin chat still waiting on the operator?" predicate. The
# scheduler re-tests it each tick for a backed-off job so the backoff clears as soon as the
# chat stops waiting (task answered / decision settled) rather than holding to the deadline.
# ``None`` until the server arms its first backoff — a scheduler-only process never arms one,
# so a missing predicate just means "hold on the deadline" (the pre-re-test behavior).
_waiting_check: Callable[[str], bool] | None = None


def set_backoff(job_id: str, generation: str, until: float) -> None:
    """Arm (or extend) the backoff for a job generation until ``until`` (epoch seconds)."""
    with _lock:
        _BACKOFF[(job_id, generation)] = until


def backoff_until(job_id: str, generation: str) -> float | None:
    """The armed backoff deadline for this generation, or ``None`` if not backed off.

    ``None`` distinguishes "never armed" from "armed but elapsed" — the server uses it
    to post the *first* still-waiting card only on the transition into backoff."""
    with _lock:
        return _BACKOFF.get((job_id, generation))


def is_backed_off(job_id: str, generation: str, now: float) -> bool:
    """Whether this job generation is currently backed off at ``now`` (epoch seconds)."""
    with _lock:
        until = _BACKOFF.get((job_id, generation))
    return until is not None and now < until


def set_waiting_check(fn: Callable[[str], bool] | None) -> None:
    """Register the predicate the scheduler re-tests to decide whether a backed-off job's
    origin chat is STILL waiting on the operator. The server injects its own waiting test
    here (it owns the tasks + decision stores); passing ``None`` detaches it."""
    global _waiting_check
    with _lock:
        _waiting_check = fn


def still_waiting(origin_session: str) -> bool | None:
    """Re-test, via the server-injected predicate, whether ``origin_session`` is still
    waiting on the operator. Returns the predicate's ``bool``, or ``None`` when no predicate
    is registered (scheduler-only process / before the first backoff is armed) so the caller
    can fall back to holding on the deadline. The predicate runs OUTSIDE the lock — it does
    store reads that must not serialize every other backoff operation behind them."""
    with _lock:
        fn = _waiting_check
    if fn is None:
        return None
    return fn(origin_session)


def clear(job_id: str, generation: str | None = None) -> None:
    """Drop the backoff for a job. With ``generation`` given, drop only that generation;
    with ``None``, drop every generation of the id (used when the job is edited)."""
    with _lock:
        if generation is None:
            for key in [k for k in _BACKOFF if k[0] == job_id]:
                del _BACKOFF[key]
        else:
            _BACKOFF.pop((job_id, generation), None)


def clear_all() -> None:
    """Drop every backoff AND the registered waiting predicate — test isolation only (the
    registry is in-process module state)."""
    global _waiting_check
    with _lock:
        _BACKOFF.clear()
        _waiting_check = None
