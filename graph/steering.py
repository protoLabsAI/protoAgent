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

import logging
import time
import uuid

log = logging.getLogger(__name__)

# session_id -> [{"id": str, "text": str, "fence"?: [str]}], FIFO.
_QUEUES: dict[str, list[dict]] = {}

# Eviction (#3933). A queue normally empties itself — the turn's next model step drains
# it, the console's ✕ dequeues it, a deleted chat ``forget``s it — but a session whose
# turn never reaches another model step (abandoned while parked, or a server-fired turn
# that ended between the interjection and its next step) kept its entry for the life of
# the process. So each queue remembers when it was last written, and every ``enqueue``
# first evicts queues untouched for ``_QUEUE_TTL_S`` and, for a NEW session at
# ``_QUEUES_MAX`` sessions, the least recently written one. The TTL is deliberately long:
# a message held behind a parked HITL form (``fence`` above) legitimately waits for the
# operator to answer it, which can be overnight — only input nobody has touched for days
# is dropped, and it is logged when it is.
_QUEUE_TTL_S = 7 * 24 * 3600.0
_QUEUES_MAX = 1024
# session_id -> monotonic time of its last enqueue, least recently written FIRST (a write
# re-inserts the key), so both evictions only ever look at the front.
_QUEUE_TOUCHED: dict[str, float] = {}


def _now() -> float:
    return time.monotonic()


def _evict(session_id: str, why: str) -> None:
    _QUEUE_TOUCHED.pop(session_id, None)
    dropped = _QUEUES.pop(session_id, None)
    if dropped:
        log.warning("[steering] dropped %d undelivered message(s) for session %s (%s)", len(dropped), session_id, why)


def _evict_stale(incoming: str) -> None:
    now = _now()
    while _QUEUE_TOUCHED:
        sid, touched = next(iter(_QUEUE_TOUCHED.items()))
        if now - touched < _QUEUE_TTL_S:
            break
        _evict(sid, f"no activity for {_QUEUE_TTL_S / 3600:g}h")
    if incoming in _QUEUES:
        return
    while len(_QUEUES) >= _QUEUES_MAX and _QUEUE_TOUCHED:
        _evict(next(iter(_QUEUE_TOUCHED)), f"queue registry at cap ({_QUEUES_MAX})")


def enqueue(session_id: str, text: str, msg_id: str | None = None, *, fence=None) -> str | None:
    """Queue a user message for ``session_id``'s running turn. Returns its id (the
    client's if supplied, else a fresh one), or None on a blank message.

    ``fence`` — the tool allowlist of the turn that sent this message (#2972), when it
    was fenced. It travels WITH the message: the pass that folds it in is narrowed to it
    (``SteeringMiddleware``), so a fenced message held behind a parked interrupt can never
    be acted on by the wider toolset of the (possibly unfenced) turn it lands in."""
    text = (text or "").strip()
    if not session_id or not text:
        return None
    mid = msg_id or uuid.uuid4().hex
    item: dict = {"id": mid, "text": text}
    if fence:
        item["fence"] = [str(t) for t in fence]
    _evict_stale(session_id)
    _QUEUES.setdefault(session_id, []).append(item)
    _QUEUE_TOUCHED.pop(session_id, None)
    _QUEUE_TOUCHED[session_id] = _now()
    return mid


def drain(session_id: str) -> list[dict]:
    """Return and clear all queued items for ``session_id`` (FIFO). Used by the
    middleware to fold the messages into the running turn.

    Each drained id is REMEMBERED (below), so a consumer can still tell "the agent read
    this" from "this never arrived" after the item has left the queue — a distinction the
    queue alone cannot make, and the live boundary marker cannot be relied on for (it is a
    best-effort callback that the sync path, or a graph invoked outside an event-stream
    context, can fail to emit)."""
    if not session_id:
        return []
    items = _QUEUES.pop(session_id, [])
    _QUEUE_TOUCHED.pop(session_id, None)
    if items:
        _note_drained(session_id, [str(item.get("id") or "") for item in items])
    return items


# session_id -> the ids most recently folded into a turn, oldest first. Bounded: this
# answers "did the agent read the message I just sent?" for as long as a console could
# still be asking, not for the life of the process (the durable record of a consumed
# interjection is the task history's steer-consumed marker).
_DRAINED: dict[str, list[str]] = {}
_DRAINED_CAP = 50
# Bounded in session COUNT too (#3940), with the same rules as ``_QUEUES``: each log
# remembers when it was last written, and every drain that records ids first evicts logs
# untouched for ``_QUEUE_TTL_S`` and, for a NEW session at ``_QUEUES_MAX`` logs, the least
# recently written one. Before this only ``forget`` (a deleted chat) ever removed a row,
# so every server-fired context that ever folded a message in kept one for the life of
# the process. Dropping a log loses nothing durable — the task history keeps the marker.
_DRAINED_TOUCHED: dict[str, float] = {}


def _evict_stale_drained(incoming: str) -> None:
    now = _now()
    while _DRAINED_TOUCHED:
        sid, touched = next(iter(_DRAINED_TOUCHED.items()))
        if now - touched < _QUEUE_TTL_S:
            break
        _DRAINED_TOUCHED.pop(sid, None)
        _DRAINED.pop(sid, None)
    if incoming in _DRAINED:
        return
    while len(_DRAINED) >= _QUEUES_MAX and _DRAINED_TOUCHED:
        sid = next(iter(_DRAINED_TOUCHED))
        _DRAINED_TOUCHED.pop(sid, None)
        _DRAINED.pop(sid, None)


def _note_drained(session_id: str, ids: list[str]) -> None:
    kept = [mid for mid in ids if mid]
    if not kept:
        return
    _evict_stale_drained(session_id)
    log = _DRAINED.setdefault(session_id, [])
    log.extend(kept)
    if len(log) > _DRAINED_CAP:
        del log[: len(log) - _DRAINED_CAP]
    _DRAINED_TOUCHED.pop(session_id, None)
    _DRAINED_TOUCHED[session_id] = _now()


def drained(session_id: str) -> list[str]:
    """Ids recently folded into a turn for ``session_id`` (oldest first)."""
    return list(_DRAINED.get(session_id, ()))


def forget(session_id: str) -> None:
    """Drop everything remembered for ``session_id`` — a deleted or cleared chat.

    ``_QUEUES`` pops itself empty on drain, but the drain log is keyed by session and would
    otherwise keep a row for every chat and server-fired context that ever folded a message
    in, for the life of the process. Called where a session is retired."""
    sid = str(session_id or "").strip()
    if not sid:
        return
    _QUEUES.pop(sid, None)
    _QUEUE_TOUCHED.pop(sid, None)
    _DRAINED.pop(sid, None)
    _DRAINED_TOUCHED.pop(sid, None)


def _reset() -> None:
    """Clear ALL of this module's process state — every queue, drain log, and their
    eviction clocks. For tests: a fixture that cleared only some of these dicts left the
    others (the ``*_TOUCHED`` clocks) to leak into the next test (#3940)."""
    _QUEUES.clear()
    _QUEUE_TOUCHED.clear()
    _DRAINED.clear()
    _DRAINED_TOUCHED.clear()


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
                _QUEUE_TOUCHED.pop(session_id, None)
            return True
    return False


def pending_items(session_id: str) -> list[dict]:
    """Peek the still-queued items for ``session_id`` (not drained) — the
    turn-end reconcile reads this to find input that arrived too late."""
    return list(_QUEUES.get(session_id, []))


def pending(session_id: str) -> int:
    """How many messages are queued for ``session_id`` (not yet drained)."""
    return len(_QUEUES.get(session_id, []))
