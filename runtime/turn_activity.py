"""Which chat sessions have a turn running RIGHT NOW — the per-session busy signal.

``server/turn_control.py``'s idle beacon (#1720) already brackets every turn driver (the A2A /
console stream and the non-streaming ``/api/chat`` / ``/v1`` path) with
``_turn_started()`` / ``_turn_ended()``, but only as a process-wide COUNT. The Zed shim
needs the per-session answer — "is the console mid-turn on this chat?" — so it can hold
a prompt instead of interleaving two turns on one thread. The same bracket now also
records the session here, and ``GET /api/chat/sessions/{id}`` reports ``active``.

A count per session (not a set): two drivers can overlap on one id (a parked HITL resume
racing a new message), and the session is only idle once BOTH have exited. In-memory,
per-process; a restart has no turns in flight by definition.
"""

from __future__ import annotations

import threading
from contextlib import asynccontextmanager

_LOCK = threading.Lock()
_ACTIVE: dict[str, int] = {}
# session id → the A2A task that holds the session's thread lock right now (#3963). A turn
# is created (and marked working) BEFORE it waits for that lock, so a queued turn's row is
# newer than the running one's; this names the one actually running.
_HOLDERS: dict[str, str] = {}


def begin(session_id: str) -> None:
    if not session_id:
        return
    with _LOCK:
        _ACTIVE[session_id] = _ACTIVE.get(session_id, 0) + 1


def end(session_id: str) -> None:
    if not session_id:
        return
    with _LOCK:
        n = _ACTIVE.get(session_id, 0) - 1
        if n > 0:
            _ACTIVE[session_id] = n
        else:
            _ACTIVE.pop(session_id, None)


def is_active(session_id: str) -> bool:
    """True while at least one turn is running on ``session_id``."""
    with _LOCK:
        return _ACTIVE.get(session_id, 0) > 0


def active_sessions() -> list[str]:
    with _LOCK:
        return sorted(_ACTIVE)


@asynccontextmanager
async def holding(session_id: str, task_id: str):
    """Record ``task_id`` as the task running on ``session_id`` for the body's duration —
    entered once the turn holds the session's thread lock. A no-op without both ids."""
    if not session_id or not task_id:
        yield
        return
    with _LOCK:
        previous = _HOLDERS.get(session_id)
        _HOLDERS[session_id] = task_id
    try:
        yield
    finally:
        with _LOCK:
            if _HOLDERS.get(session_id) == task_id:
                if previous:
                    _HOLDERS[session_id] = previous
                else:
                    _HOLDERS.pop(session_id, None)


def holding_task(session_id: str) -> str | None:
    """The A2A task running on ``session_id`` (holding its thread lock), or None."""
    with _LOCK:
        return _HOLDERS.get(session_id)
