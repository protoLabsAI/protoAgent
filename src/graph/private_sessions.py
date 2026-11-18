"""Which conversations carry private (human-operator) turns — the write-side memory gate.

A privately-steered turn's input and reply stay in the conversation's checkpoint, where a
later ordinary turn, an idle gist, a compaction archive or the retirement harvest would
otherwise derive peer-readable session memory from them. Checkpoint messages carry no
per-turn audience, so the protection follows the conversation: once a private turn has run
on a session, the shared derived writers skip it — the same shape as the incognito skip,
but durable, because a later ordinary turn cannot clear it.

The mark is a content-free file under the session-memory dir, named by a digest of the
session id (any id is safe as a filename, and the listing walkers only read ``*.json``
at the top level). A failed write falls back to an in-process mark and logs loudly.
A conversation that never had a private turn has no mark: every writer is unchanged.
"""

from __future__ import annotations

import hashlib
import logging
import os

from graph.fence_scope import in_private_segment

log = logging.getLogger(__name__)

_DIR = ".private-sessions"
_fallback: set[str] = set()


def _marker(session_id: str) -> str:
    from graph.middleware.memory import memory_path

    return os.path.join(memory_path(), _DIR, hashlib.sha256(session_id.encode("utf-8")).hexdigest())


def mark(session_id: str) -> None:
    """Record that ``session_id`` has a private turn. Idempotent; never raises."""
    if not session_id:
        return
    try:
        path = _marker(session_id)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "a", encoding="utf-8"):
            pass
    except Exception:
        _fallback.add(session_id)
        log.exception("[memory] could not persist the private-session mark; holding it in-process")


def is_marked(session_id: str) -> bool:
    if not session_id:
        return False
    if session_id in _fallback:
        return True
    try:
        return os.path.exists(_marker(session_id))
    except Exception:
        return True  # an unreadable mark must not open the shared writers


def withheld(session_id: str) -> bool:
    """Whether shared derived memory must NOT be written for ``session_id`` right now: a
    private turn is running (it is marked on the way), or one ran earlier."""
    if in_private_segment():
        mark(session_id)
        return True
    return is_marked(session_id)
