"""Mid-turn user steering — a per-session queue of user messages that get folded
into a RUNNING turn at the next model call.

The console enqueues via ``POST /api/chat/sessions/{id}/steer`` while a turn is
streaming; ``SteeringMiddleware`` (graph/middleware/steering.py) drains the queue
in ``before_model`` and appends the messages, so the model sees the new input on
its next step — letting a user redirect or reset ongoing work without stopping the
stream.

Each item carries a client-supplied ``id`` so the console can reconcile at
turn-end: anything still queued when the turn finishes arrived after the last
model call (not consumed) and is re-sent as a fresh turn; the rest were folded in.

A process-wide singleton dict is fine: the graph turn and the API endpoint run in
the same process + event loop, so enqueue (API) and drain (graph) never race on
the dict. host-free (lives under graph/), so both the middleware and operator_api
can import it without crossing an import layer.
"""

from __future__ import annotations

import uuid

# session_id -> [{"id": str, "text": str}], FIFO.
_QUEUES: dict[str, list[dict]] = {}


def enqueue(session_id: str, text: str, msg_id: str | None = None) -> str | None:
    """Queue a user message for ``session_id``'s running turn. Returns its id (the
    client's if supplied, else a fresh one), or None on a blank message."""
    text = (text or "").strip()
    if not session_id or not text:
        return None
    mid = msg_id or uuid.uuid4().hex
    _QUEUES.setdefault(session_id, []).append({"id": mid, "text": text})
    return mid


def drain(session_id: str) -> list[dict]:
    """Return and clear all queued items for ``session_id`` (FIFO). Used by the
    middleware to fold the messages into the running turn."""
    if not session_id:
        return []
    return _QUEUES.pop(session_id, [])


def dequeue(session_id: str, msg_id: str) -> bool:
    """Remove a still-queued steering message by id BEFORE it's folded in (the
    console's ✕-cancel on a pending bubble). Returns True if it was found and
    dropped; False if absent — already drained into the running turn (too late;
    the agent will still act on it) or never queued. The console settles a
    not-removed steer into the thread rather than lie that it never happened."""
    if not session_id or not msg_id:
        return False
    q = _QUEUES.get(session_id)
    if not q:
        return False
    for i, item in enumerate(q):
        if item.get("id") == msg_id:
            del q[i]
            if not q:
                _QUEUES.pop(session_id, None)  # match drain(): no empty lists linger
            return True
    return False


def pending_items(session_id: str) -> list[dict]:
    """Peek the still-queued items for ``session_id`` (not drained) — the
    turn-end reconcile reads this to find input that arrived too late."""
    return list(_QUEUES.get(session_id, []))


def pending(session_id: str) -> int:
    """How many messages are queued for ``session_id`` (not yet drained)."""
    return len(_QUEUES.get(session_id, []))
