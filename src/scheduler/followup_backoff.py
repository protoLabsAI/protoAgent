"""Follow-up backoff registry (ADR 0129 S14, #5270).

A recurring follow-up whose origin chat is *waiting on the operator* and that keeps
reporting nothing new backs off to one fire per hour instead of re-reporting the same
parked work every slot. Two subsystems share this registry:

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
clean (its new generation has no entry). Local schedulers register their existing
jobs stores at construction; deadlines are loaded and written through to those stores.
Reads refresh from SQLite so cancellation or edits by another producer cannot leave a
stale cached deadline. Without a registered store the registry remains in-memory.
"""

from __future__ import annotations

import sqlite3
import threading
from collections.abc import Callable
from contextlib import closing
from weakref import WeakSet

from infra.sqlite_locks import file_lock

_lock = threading.Lock()

# (job_id, generation) -> POSIX timestamp (UTC epoch seconds) until which the recurring
# job's due occurrences are skipped. Set by the server when a quiet follow-up whose chat
# is waiting hits the unchanged-run threshold; read by the scheduler each tick.
_BACKOFF: dict[tuple[str, str], float] = {}
_stores: WeakSet = WeakSet()


def _register_store(store) -> None:
    """Attach an existing scheduler DB and load its deadlines before polling."""
    path = store.path
    with _lock, closing(sqlite3.connect(path)) as db:
        _stores.add(store)
        rows = db.execute(
            "SELECT id, created_at, followup_backoff_until FROM jobs"
        ).fetchall()
        for job_id, generation, until in rows:
            if until is not None:
                _BACKOFF[(job_id, generation)] = until


def _write(
    job_id: str, generation: str | None, until: float | None, *, expired_by: float | None = None,
) -> None:
    # Caller holds the registry lock. Never invoke this while holding a store lock.
    for path in {store.path for store in _stores}:
        with file_lock(path), closing(sqlite3.connect(path)) as db, db:
            sql = "UPDATE jobs SET followup_backoff_until = ? WHERE id = ?"
            args = [until, job_id]
            if generation is not None:
                sql += " AND created_at = ?"
                args.append(generation)
            if expired_by is not None:
                sql += " AND followup_backoff_until <= ?"
                args.append(expired_by)
            db.execute(sql, args)


def _read(job_id: str, generation: str) -> float | None:
    if not _stores:
        return _BACKOFF.get((job_id, generation))
    for path in {store.path for store in _stores}:
        with closing(sqlite3.connect(path)) as db:
            row = db.execute(
                "SELECT followup_backoff_until FROM jobs WHERE id = ? AND created_at = ?",
                (job_id, generation),
            ).fetchone()
        if row is not None:
            return row[0]
    return None


# The server-injected "is this origin chat still waiting on the operator?" predicate. The
# scheduler re-tests it each tick for a backed-off job so the backoff clears as soon as the
# chat stops waiting (task answered / decision settled) rather than holding to the deadline.
# ``None`` until the server arms its first backoff — a scheduler-only process never arms one,
# so a missing predicate just means "hold on the deadline" (the pre-re-test behavior).
_waiting_check: Callable[[str], bool] | None = None


def set_backoff(job_id: str, generation: str, until: float) -> None:
    """Arm (or extend) the backoff for a job generation until ``until`` (epoch seconds)."""
    with _lock:
        _write(job_id, generation, until)
        _BACKOFF[(job_id, generation)] = until


def backoff_until(job_id: str, generation: str) -> float | None:
    """The armed backoff deadline for this generation, or ``None`` if not backed off.

    ``None`` distinguishes "never armed" from "armed but elapsed" — the server uses it
    to post the *first* still-waiting card only on the transition into backoff."""
    with _lock:
        return _read(job_id, generation)


def is_backed_off(job_id: str, generation: str, now: float) -> bool:
    """Whether this job generation is currently backed off at ``now`` (epoch seconds)."""
    with _lock:
        until = _read(job_id, generation)
        if until is not None and now >= until:
            # Another process may have extended the deadline since the read.
            _write(job_id, generation, None, expired_by=now)
            _BACKOFF.pop((job_id, generation), None)
            until = _read(job_id, generation)
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
        _write(job_id, generation, None)
        if generation is None:
            for key in [k for k in _BACKOFF if k[0] == job_id]:
                del _BACKOFF[key]
        else:
            _BACKOFF.pop((job_id, generation), None)


def clear_all() -> None:
    """Reset process state and detach stores/predicate for test isolation.

    Persisted deadlines are untouched, allowing tests to simulate a restart.
    """
    global _waiting_check
    with _lock:
        _BACKOFF.clear()
        _stores.clear()
        _waiting_check = None
