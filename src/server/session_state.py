"""Server-held per-session live state (ADR 0130 S2, D1).

The server — not any client — owns what each chat session is doing:
``{live_task_id, status: idle|running|paused, last_change_seq, updated_at}``,
derived from the A2A task store and the turn lock (``runtime.turn_activity``),
never from a client's local guess. ``GET /api/chat/sessions`` and
``GET /api/chat/sessions/{id}`` expose it (via the ``server.chat`` re-exports —
``operator_api`` may not import this module directly), and every transition
publishes ONE ``session.changed`` bus event, which the S1 change log records
durably, so a resuming client replays the transitions it missed and refetches
the session (D2: events carry identities; the GET is the truth).

Transitions and their chokepoints:

- **turn start** — the idle-beacon bracket's 0→1 edge (``turn_control._turn_started``;
  every driver passes through it: console stream, A2A, ``/api/chat``, ``/v1``,
  server-fired loopback turns). ``status: running``.
- **turn end** — the bracket's →0 edge. The end state is DERIVED, not guessed:
  the session may be idle (normal), or still paused (the leg parked — the pause
  below already published, and the dedupe keeps this edge silent).
- **pause** — the executor's ``input_required`` progress frame (a HITL form /
  approval / ``request_decision`` park). ``status: paused``.
- **steer consumed** — the executor's ``steer_consumed`` frame: status is
  unchanged (still running) but clients must settle their queued bubbles, so it
  always publishes (``force``).
- **delete** — the session delete route. ``status: deleted`` (event-only; the
  API answers 404 from then on).

A steer posted to an idle session starts a fresh turn (bd-0ad5v,
``server.chat.steer_or_start_turn``) and is covered by the bracket like any
other turn. Statuses are deduped per session so one logical transition never
publishes twice; ``last_change_seq`` is the bus seq of the session's newest
``session.changed`` — tracked live by a bus handler and reseeded from the
durable change log after a restart, so D3's "compare ``last_change_seq``"
backstop survives the process.

Known, deliberate gap: a HITL park on the NON-streaming driver (``/api/chat``)
leaves no A2A task row, so it derives ``idle`` — the same blindness
``session_summary.last_state`` has today. Execution contexts owned by jobs
(``background:…``, ``job:…``) are not chat sessions and never publish here.
"""

from __future__ import annotations

import asyncio
import logging
import re
import threading
import time
from datetime import datetime, timezone
from typing import Any

from runtime import turn_activity
from runtime.state import STATE

log = logging.getLogger("protoagent.server")

TOPIC = "session.changed"

#: The D1 status vocabulary. ``deleted`` appears only on the event — the API
#: answers 404 for a deleted session, never a body carrying it.
STATUS_IDLE = "idle"
STATUS_RUNNING = "running"
STATUS_PAUSED = "paused"
STATUS_DELETED = "deleted"

# The stored task-state spellings (A2A 1.0 and the 0.3 rows an older store wrote) —
# the same classifiers operator_api.chat_routes applies to ``last_state``.
_TERMINAL_STATE = re.compile(r"completed|failed|canceled|cancelled|rejected", re.IGNORECASE)
_PAUSED_STATE = re.compile(r"input.required|auth.required", re.IGNORECASE)

# Execution contexts owned by background jobs (ADR 0050 / ADR 0129): not chat
# sessions — no composer, no session row — so no session state is published for them.
_NON_SESSION_PREFIXES = ("background:", "job:")

_LOCK = threading.Lock()
#: session → (status, live_task_id) last published; the once-per-transition dedupe.
_LAST: dict[str, tuple[str, str]] = {}
#: session → transition counter. An async settle snapshots it and publishes only if
#: no newer transition landed while it was reading the task store.
_EPOCH: dict[str, int] = {}
#: session → (seq, ts) of its newest ``session.changed`` — fed live by the bus
#: handler below, reseeded once per process from the durable change log.
_SEQS: dict[str, tuple[int, float]] = {}
_WIRED = False
_SEEDED = False
# Strong refs to in-flight settle tasks (asyncio holds tasks weakly).
_SETTLES: set[asyncio.Task] = set()


def _bus():
    """The server's bus, at call time (the module import order mirrors turn_control:
    nothing from ``server`` is imported at import time, and a test's monkeypatched
    ``server._event_bus`` is picked up)."""
    from server import _event_bus

    return _event_bus


def is_chat_session(session_id: str) -> bool:
    """Whether ``session_id`` names a session this module tracks (not blank, not a
    job-owned execution context)."""
    sid = str(session_id or "").strip()
    return bool(sid) and not sid.startswith(_NON_SESSION_PREFIXES)


def _record_seq(payload: dict[str, Any]) -> Any:
    """Bus handler: remember each session's newest ``session.changed`` seq (sync, so
    it lands before the publish returns on-loop)."""
    data = payload.get("data") or {}
    sid = str(data.get("session_id") or "")
    if not sid:
        return
    with _LOCK:
        _SEQS[sid] = (int(payload.get("seq") or 0), float(payload.get("ts") or time.time()))


def _ensure_wired() -> None:
    global _WIRED
    with _LOCK:
        if _WIRED:
            return
        _WIRED = True
    try:
        _bus().subscribe_handler(TOPIC, _record_seq)
    except Exception:  # noqa: BLE001 — a missing bus (early boot) must not break a turn
        with _LOCK:
            _WIRED = False


def _ensure_seeded() -> None:
    """Reseed ``_SEQS`` once per process from the durable change log (one grouped
    scan), so ``last_change_seq`` survives a restart. A bare bus (no log) seeds empty."""
    global _SEEDED
    with _LOCK:
        if _SEEDED:
            return
        _SEEDED = True
    try:
        clog = _bus().change_log()
    except Exception:  # noqa: BLE001 — no bus yet: nothing durable to seed from
        return
    if clog is None:
        return
    try:
        seeded = clog.last_entity_seqs(TOPIC)
    except Exception:  # noqa: BLE001 — an unreadable log degrades to live-only seqs
        log.warning("[session-state] could not reseed last_change_seq from the change log", exc_info=True)
        return
    with _LOCK:
        for sid, pair in seeded.items():
            _SEQS.setdefault(sid, pair)


def last_change_seq(session_id: str) -> tuple[int | None, float | None]:
    """``(seq, ts)`` of the session's newest ``session.changed``, or ``(None, None)``."""
    _ensure_wired()
    _ensure_seeded()
    with _LOCK:
        pair = _SEQS.get(str(session_id or ""))
    return pair if pair else (None, None)


def derive_status(
    *,
    active: bool,
    holder: str | None,
    newest_state: str | None,
    newest_task_id: str | None,
) -> tuple[str, str]:
    """``(status, live_task_id)`` from the turn lock + the session's newest-created turn.

    The turn lock is the ground truth for RUNNING: a turn in flight in this process wins
    over whatever the store says (its row may still read ``working`` or already be
    settled). With nothing in flight, the newest-created row decides — parked
    (input/auth-required) ⇒ ``paused`` (the task awaiting the operator IS the live task);
    anything else ⇒ ``idle``. A ``working`` row with no live turn is a turn this process
    is not running (a restart cut it down; D7 recovery will settle it) and reads ``idle``
    — never a phantom ``running`` no client could stop. Orphaned parks behind a finished
    turn read idle too, matching ``_live_turn``'s trailing-run rule.
    """
    if active:
        live = str(holder or "")
        if not live and newest_task_id and newest_state and not _TERMINAL_STATE.search(newest_state):
            live = str(newest_task_id)
        return STATUS_RUNNING, live
    if newest_state and _PAUSED_STATE.search(newest_state):
        return STATUS_PAUSED, str(newest_task_id or "")
    return STATUS_IDLE, ""


def _utc(value: Any) -> datetime | None:
    """A store timestamp as an aware UTC datetime (naive = UTC by the store contract)."""
    if value is None:
        return None
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value)
        except ValueError:
            return None
    if not isinstance(value, datetime):
        return None
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def live_state_fields(
    session_id: str,
    *,
    newest_state: str | None = None,
    newest_task_id: str | None = None,
    newest_updated: Any = None,
) -> dict[str, Any]:
    """The D1 state fields for one session, from values a route already read.

    ``newest_*`` are the session's newest-created turn (stored state spelling, task id,
    ``last_updated``) — the sessions index's windowed pass and the summary read both
    already have them, so this adds no query. ``updated_at`` is the newer of the stored
    turn's ``last_updated`` and the last published transition, zoned ISO-8601 UTC.
    """
    sid = str(session_id or "")
    status, live = derive_status(
        active=turn_activity.is_active(sid),
        holder=turn_activity.holding_task(sid),
        newest_state=newest_state,
        newest_task_id=newest_task_id,
    )
    seq, seq_ts = last_change_seq(sid)
    transition_at = datetime.fromtimestamp(seq_ts, tz=timezone.utc) if seq_ts else None
    stamps = [t for t in (_utc(newest_updated), transition_at) if t]
    return {
        "status": status,
        "live_task_id": live or None,
        "last_change_seq": seq,
        "updated_at": max(stamps).isoformat() if stamps else None,
    }


async def _newest_turn(session_id: str) -> tuple[str | None, str | None, Any]:
    """``(state, task_id, last_updated)`` of the session's newest-created stored turn
    (``(None, None, None)`` without a task store or rows)."""
    engine = getattr(STATE, "a2a_task_engine", None)
    if engine is None:
        return None, None, None
    from sqlalchemy import select

    from a2a.server.tasks.database_task_store import TaskModel
    from a2a_impl.stores import task_newest_first

    async with engine.begin() as conn:
        row = (
            await conn.execute(
                select(TaskModel.id, TaskModel.status, TaskModel.last_updated)
                .where(TaskModel.context_id == session_id)
                .order_by(*task_newest_first(TaskModel, conn.dialect.name))
                .limit(1)
            )
        ).first()
    if row is None:
        return None, None, None
    state = (row.status or {}).get("state") if isinstance(row.status, dict) else None
    return (state or None), row.id, row.last_updated


def publish_session_changed(
    session_id: str, status: str, live_task_id: str = "", *, reason: str = "", force: bool = False
) -> bool:
    """Publish ONE ``session.changed`` transition — deduped on ``(status, live_task_id)``
    so a settle that confirms the already-published state stays silent. ``force`` is for
    transitions that change the session without changing its status (a consumed steer).
    Returns whether an event was published. Never raises — callers sit inside turn
    brackets and executor hooks."""
    sid = str(session_id or "").strip()
    if not is_chat_session(sid):
        return False
    _ensure_wired()
    tid = str(live_task_id or "")
    with _LOCK:
        if not force and _LAST.get(sid) == (status, tid):
            return False
        if status == STATUS_DELETED:
            _LAST.pop(sid, None)
        else:
            _LAST[sid] = (status, tid)
        _EPOCH[sid] = _EPOCH.get(sid, 0) + 1
    try:
        _bus().publish(
            TOPIC,
            {
                "session_id": sid,
                "status": status,
                "live_task_id": tid,
                "reason": reason,
                "updated_at": datetime.now(timezone.utc).isoformat(),
            },
        )
    except Exception:  # noqa: BLE001 — a bus failure must never break the turn
        log.exception("[session-state] session.changed publish failed for %s", sid)
        return False
    return True


# ── transition chokepoints ───────────────────────────────────────────────────


def note_turn_started(session_id: str) -> None:
    """The turn bracket's 0→1 edge (``turn_control._turn_started``): the session is
    running, whatever surface drives the turn."""
    try:
        publish_session_changed(
            session_id, STATUS_RUNNING, turn_activity.holding_task(str(session_id or "")) or "", reason="turn_started"
        )
    except Exception:  # noqa: BLE001 — never into the bracket
        log.exception("[session-state] turn-start transition failed for %s", session_id)


def note_turn_ended(session_id: str) -> None:
    """The bracket's →0 edge. The end state is derived from the task store + turn lock
    (a parked leg leaves the session ``paused``, not ``idle``), so the publish settles
    asynchronously; with no loop (sync tests, CLI) it degrades to the in-memory view."""
    sid = str(session_id or "").strip()
    if not is_chat_session(sid):
        return
    with _LOCK:
        epoch = _EPOCH.get(sid, 0)
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        with _LOCK:
            paused = _LAST.get(sid, ("", ""))[0] == STATUS_PAUSED
        if not paused:
            publish_session_changed(sid, STATUS_RUNNING if turn_activity.is_active(sid) else STATUS_IDLE, reason="turn_ended")
        return
    task = loop.create_task(_settle(sid, epoch, reason="turn_ended"))
    _SETTLES.add(task)
    task.add_done_callback(_SETTLES.discard)


async def _settle(session_id: str, epoch: int, *, reason: str) -> None:
    """Derive-and-publish, unless a newer transition already told the story."""
    try:
        state, task_id, _updated = await _newest_turn(session_id)
    except Exception:  # noqa: BLE001 — an unreadable store settles nothing (the GET derives fresh)
        log.warning("[session-state] settle read failed for %s", session_id, exc_info=True)
        return
    status, live = derive_status(
        active=turn_activity.is_active(session_id),
        holder=turn_activity.holding_task(session_id),
        newest_state=state,
        newest_task_id=task_id,
    )
    with _LOCK:
        if _EPOCH.get(session_id, 0) != epoch:
            return
    publish_session_changed(session_id, status, live, reason=reason)


def settle_session(session_id: str) -> None:
    """Re-derive and publish a session's state out of band (restart recovery settling an
    interrupted task, or any writer that changed the store without a turn bracket)."""
    sid = str(session_id or "").strip()
    if not is_chat_session(sid):
        return
    with _LOCK:
        epoch = _EPOCH.get(sid, 0)
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    task = loop.create_task(_settle(sid, epoch, reason="settle"))
    _SETTLES.add(task)
    task.add_done_callback(_SETTLES.discard)


def note_paused(session_id: str, task_id: str) -> None:
    """The executor's ``input_required`` frame: the turn parked on a HITL form /
    approval / decision and ``task_id`` is the task awaiting the operator."""
    try:
        publish_session_changed(session_id, STATUS_PAUSED, task_id, reason="paused")
    except Exception:  # noqa: BLE001 — never into the executor
        log.exception("[session-state] pause transition failed for %s", session_id)


def note_steer_consumed(session_id: str, task_id: str) -> None:
    """The executor's ``steer_consumed`` frame: queued operator input folded into the
    running turn. Status is unchanged, but clients settle their queued bubbles off the
    session snapshot, so this transition always publishes."""
    try:
        publish_session_changed(session_id, STATUS_RUNNING, task_id, reason="steer_consumed", force=True)
    except Exception:  # noqa: BLE001 — never into the executor
        log.exception("[session-state] steer-consumed transition failed for %s", session_id)


def note_session_deleted(session_id: str) -> None:
    """The session delete route: the session is gone (clients refetch and get the 404)."""
    try:
        publish_session_changed(session_id, STATUS_DELETED, reason="deleted", force=True)
    except Exception:  # noqa: BLE001 — the delete already happened; the event is best-effort
        log.exception("[session-state] delete transition failed for %s", session_id)


def _reset_for_tests() -> None:
    """Clear the in-process maps (NOT the durable log) — a fresh process, for tests."""
    global _WIRED, _SEEDED
    with _LOCK:
        _LAST.clear()
        _EPOCH.clear()
        _SEQS.clear()
        _WIRED = False
        _SEEDED = False
