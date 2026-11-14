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

It lives in ``scheduler`` (not ``server``) because the scheduler must never import the
server bootstrap (import-layering contract). Keyed by ``(job_id, generation)`` — the
generation is the job's ``created_at``, so a cancel-and-recreate on the same id starts
clean (its new generation has no entry). Purely in memory: a restart clears every
backoff, so at worst one extra card posts after a restart, never a missed resume.
"""

from __future__ import annotations

import threading

_lock = threading.Lock()

# (job_id, generation) -> POSIX timestamp (UTC epoch seconds) until which the recurring
# job's due occurrences are skipped. Set by the server when a quiet follow-up whose chat
# is waiting hits the unchanged-run threshold; read by the scheduler each tick.
_BACKOFF: dict[tuple[str, str], float] = {}


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
    """Drop every backoff — test isolation only (the registry is in-process module state)."""
    with _lock:
        _BACKOFF.clear()
