"""Cancellable subagent delegations — a per-session registry of in-flight
foreground ``task`` delegations so the console can ABORT one without killing the
whole turn (mid-turn steering, Tier 2).

The lead's ``task`` tool normally ``await``s a subagent to completion
(graph/agent.py), blocking the turn with no cancel handle. We wrap that await in
an ``asyncio.Task``, register it here keyed by the **tool-call id** the console
already sees on the running ``task`` tool card, and expose ``cancel()`` so
``POST …/delegations/{id}/cancel`` can ``.cancel()`` it. The tool then returns a
graceful "cancelled" string and the LEAD CONTINUES — unlike an A2A ``CancelTask``,
which kills the entire turn (that's what the composer Stop button does).

``cancel()`` sets a per-entry ``cancelled`` flag BEFORE cancelling the task so the
tool can tell a *user-initiated delegation cancel* (swallow ``CancelledError`` →
graceful return, lead continues) from a *parent turn-level cancel* (flag unset →
re-raise so the whole turn dies as intended).

A process-wide singleton dict is fine: the graph turn and the API endpoint run in
the same process + event loop, so register (graph) and cancel (API) never race on
the dict. host-free (lives under graph/) so both agent.py and operator_api can
import it without crossing an import layer — mirrors graph/steering.py.
"""

from __future__ import annotations

from typing import Any

# session_id -> { delegation_id: {"task": <asyncio task/future>, "label": str, "cancelled": bool} }
_RUNNING: dict[str, dict[str, dict]] = {}


def register(session_id: str, delegation_id: str, task: Any, *, label: str = "") -> None:
    """Track an in-flight foreground delegation so it can be cancelled by id. A
    blank ``session_id``/``delegation_id`` (e.g. the tool ran without an injected
    tool-call id) is a no-op — the delegation simply isn't cancellable."""
    if not session_id or not delegation_id:
        return
    _RUNNING.setdefault(session_id, {})[delegation_id] = {
        "task": task,
        "label": label,
        "cancelled": False,
    }


def unregister(session_id: str, delegation_id: str) -> None:
    """Drop a delegation from the registry once it has settled (call in a finally).
    Pops the empty session dict so nothing lingers, like ``steering.drain``."""
    sess = _RUNNING.get(session_id)
    if not sess:
        return
    sess.pop(delegation_id, None)
    if not sess:
        _RUNNING.pop(session_id, None)


def cancel(session_id: str, delegation_id: str) -> bool:
    """Abort a running delegation by id. Marks it ``cancelled`` (so the tool returns
    a graceful 'cancelled' message instead of failing the turn) and cancels its
    task. Returns True if a live, not-already-cancelled delegation was found; False
    if absent, already finished, or already cancelling (too late / nothing to do)."""
    entry = (_RUNNING.get(session_id) or {}).get(delegation_id)
    if not entry or entry["cancelled"]:
        return False
    task = entry["task"]
    if task.done():
        return False
    entry["cancelled"] = True
    task.cancel()
    return True


def was_cancelled(session_id: str, delegation_id: str) -> bool:
    """True if this delegation was explicitly cancelled via ``cancel()`` (vs a
    parent turn-level ``CancelledError`` that merely passed through). The tool reads
    this to decide swallow-and-continue vs re-raise."""
    entry = (_RUNNING.get(session_id) or {}).get(delegation_id)
    return bool(entry and entry["cancelled"])


def running_items(session_id: str) -> list[dict]:
    """The in-flight delegations for a session — ``[{"id", "label"}]`` — the
    authoritative list the console's Cancel affordance can act on / verify."""
    return [{"id": did, "label": e["label"]} for did, e in (_RUNNING.get(session_id) or {}).items()]


def running(session_id: str) -> int:
    """How many foreground delegations are in flight for ``session_id``."""
    return len(_RUNNING.get(session_id) or {})
