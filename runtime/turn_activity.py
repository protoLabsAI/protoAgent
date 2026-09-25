"""Which chat sessions have a turn running RIGHT NOW — the per-session busy signal.

``server/chat.py``'s idle beacon (#1720) already brackets every turn driver (the A2A /
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

_LOCK = threading.Lock()
_ACTIVE: dict[str, int] = {}


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
