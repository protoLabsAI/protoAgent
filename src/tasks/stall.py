"""Is anything driving this task? — the shared driver test + stall finder (ADR 0129 D9).

A task's status is a claim; nothing used to verify the work behind an ``in_progress``
task is actually moving (D9: ``task-59`` sat ``in_progress`` for an afternoon with no
linked work, schedule, watch or background job — nothing that would ever bring the
agent back to it). This module answers the one question every D9 consumer asks —
"is this task *driven*?" — so the stall check, the Work view status and the
``task_update`` warning can never disagree.

A task is **driven** when ANY of these holds:

* it has a ``links`` entry that is not yet resolved — a board card, PR, scheduler job
  or directive the task carries (``tasks.store`` vocabulary: a link is resolved when
  its ``state`` is ``done``);
* a pending scheduler job (one-shot or ``wait``) belongs to the task's ``session_id``
  (``job.context_id`` or ``job.origin_session`` matches);
* an active watch belongs to that session;
* a foreground delegation or a background job is running for that session;
* a turn is running in that session.

**Fail quiet.** The point-in-time checks read live, best-effort state. A source that
raises counts as *driven* — never nudge a task on missing evidence (a scheduler DB
hiccup must not look like "nobody is driving this"). An empty ``session_id`` is judged
on links alone (the session-scoped sources can't match it).

This is the pure, injectable half (ADR 0129 S11a): ``is_driven`` / ``find_stalled``
take a ``sources`` bag of callables so tests inject fakes, and :func:`default_sources`
builds the live ones from the process :data:`runtime.state.STATE`. The sweep loop and
server wiring that call this live land in S11b.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Callable

from tasks.store import parse_links

# ── D9 constants (written as constants, not settings, for now — ADR 0129 "Defaults") ──
#: An undriven ``in_progress`` task older than this (seconds) is stalled — 30 minutes.
STALL_THRESHOLD_S = 1800
#: After this many nudges a still-stalled task is parked in ``waiting`` instead of a
#: fourth nudge (``TaskStore.record_stall_nudge`` returning > this → the cap is reached).
STALL_NUDGE_CAP = 3
#: Cadence the stall sweep runs on (seconds) — 5 minutes, the task link sweep's cadence.
STALL_SWEEP_INTERVAL_S = 300


@dataclass
class DriverSources:
    """The live signals the driver test reads, as callables so tests inject fakes.

    ``list_jobs`` / ``active_watches`` return the raw rows (``is_driven`` matches them to
    the session); the rest take a ``session_id`` and answer directly. Each is called
    best-effort — a raise is treated as "driven" by :func:`is_driven`."""

    #: All pending scheduler jobs (Job-like: ``.context_id`` / ``.origin_session``).
    list_jobs: Callable[[], Any]
    #: The currently-active watches (Watch-like: ``.run_session``).
    active_watches: Callable[[], Any]
    #: Count of in-flight foreground delegations for the session.
    running_delegations: Callable[[str], int]
    #: Count of background jobs still running for the session.
    running_background: Callable[[str], int]
    #: Whether a turn is running in the session right now.
    turn_running: Callable[[str], Any]


def _source(sources: Any, name: str) -> Callable[..., Any]:
    """The named callable from a :class:`DriverSources` (or a plain dict of callables).
    A missing name raises — which :func:`is_driven` treats as driven (fail quiet)."""
    if isinstance(sources, Mapping):
        return sources[name]
    return getattr(sources, name)


def _field(obj: Any, name: str) -> Any:
    """A field off a row that may be a dataclass/namespace or a plain dict (fakes)."""
    if isinstance(obj, Mapping):
        return obj.get(name)
    return getattr(obj, name, None)


def _job_drives(sources: Any, session_id: str) -> bool:
    """A pending scheduler job whose context or origin is this session (includes
    ``wait:`` jobs — ``list_jobs`` returns them and nothing filters them out)."""
    for job in _source(sources, "list_jobs")() or []:
        if _field(job, "context_id") == session_id or _field(job, "origin_session") == session_id:
            return True
    return False


def _watch_drives(sources: Any, session_id: str) -> bool:
    """An active watch that reacts in this session (``run_session``)."""
    for watch in _source(sources, "active_watches")() or []:
        if _field(watch, "run_session") == session_id:
            return True
    return False


def is_driven(task: dict, *, sources: Any) -> bool:
    """Whether anything is driving ``task`` right now (ADR 0129 D9 item 1).

    ``True`` when an unresolved link, a session-scoped pending job / active watch /
    running delegation / running background job, or a live turn in the session backs
    the task. A ``sources`` callable that raises counts as driven (fail quiet: missing
    evidence is never a reason to nudge). A blank ``session_id`` is judged on links
    alone — the session-scoped sources have nothing to match."""
    # An unresolved link is durable evidence and needs no live source — a link is
    # resolved when its state is ``done`` (tasks.store.resolve_link vocabulary).
    if any(entry.get("state") != "done" for entry in parse_links(task)):
        return True

    session_id = str(task.get("session_id") or "").strip()
    if not session_id:
        return False

    try:
        return (
            _job_drives(sources, session_id)
            or _watch_drives(sources, session_id)
            or _source(sources, "running_delegations")(session_id) > 0
            or _source(sources, "running_background")(session_id) > 0
            or bool(_source(sources, "turn_running")(session_id))
        )
    except Exception:  # noqa: BLE001 — a source we can't read is not proof of a stall
        return True


def find_stalled(
    tasks: Any,
    *,
    now: datetime | str,
    sources: Any,
    threshold_s: float = STALL_THRESHOLD_S,
) -> list[dict]:
    """The stalled tasks among ``tasks`` (ADR 0129 D9 item 2): status ``in_progress``,
    not driven, and ``updated_at`` older than ``threshold_s`` relative to ``now``.

    A task whose ``updated_at`` can't be parsed is skipped (can't prove it's old →
    don't nudge). The driver test runs only for old-enough tasks, so a busy board
    doesn't poll the live sources for every recently-touched row."""
    stalled: list[dict] = []
    for task in tasks:
        if (task.get("status") or "") != "in_progress":
            continue
        age = _age_seconds(now, task.get("updated_at"))
        if age is None or age <= threshold_s:
            continue
        if is_driven(task, sources=sources):
            continue
        stalled.append(task)
    return stalled


def task_activity(
    task: dict,
    *,
    now: datetime | str,
    sources: Any,
    threshold_s: float = STALL_THRESHOLD_S,
) -> str:
    """The D9 activity of an ``in_progress`` ``task`` (ADR 0129 D9 item 5), for the task
    API and the Work view: ``"driven"`` when something is driving it, ``"stalled"`` when
    nothing is and it has gone untouched past ``threshold_s``, else ``"idle"``.

    Shares :func:`is_driven` and the ``updated_at`` read with :func:`find_stalled`, so the
    API label, the stall sweep and the ``task_update`` warning can never disagree (that
    agreement is this module's whole reason for existing). An ``updated_at`` that can't be
    read is ``"idle"`` — never claim a stall we can't prove, mirroring ``find_stalled``'s
    skip of unparseable rows. The caller decides which statuses get a label; this answers
    only for the in-progress ones."""
    if is_driven(task, sources=sources):
        return "driven"
    age = _age_seconds(now, task.get("updated_at"))
    if age is not None and age > threshold_s:
        return "stalled"
    return "idle"


def default_sources() -> DriverSources:
    """Build the live :class:`DriverSources` from the process :data:`runtime.state.STATE`.

    Each callable is lazy and None-safe — a disabled subsystem (no scheduler, no watch
    controller, no background manager wired) reports *no* evidence from that source
    rather than raising, so it reads as "not driving" for that signal. A real read
    failure inside a wired subsystem still raises, which :func:`is_driven` treats as
    driven. Imports are deferred so this host-free module stays importable without the
    runtime (tests inject fakes and never call this)."""
    from graph import delegations
    from runtime import turn_activity
    from runtime.state import STATE

    def list_jobs() -> Any:
        sched = STATE.scheduler
        if sched is None:
            return []
        # Read via the strict path so a scheduler DB read failure RAISES (→ is_driven
        # treats the task as driven) instead of list_jobs()'s swallowed empty list,
        # which would read as "no pending job" and could nudge a task that is in fact
        # being driven (D9 fail-quiet — never nudge on missing evidence). A backend
        # without a strict reader falls back to list_jobs().
        reader = getattr(sched, "list_jobs_strict", None) or sched.list_jobs
        return reader()

    def active_watches() -> Any:
        wc = STATE.watch_controller
        return wc.active_watches() if wc is not None else []

    def running_delegations(session_id: str) -> int:
        return delegations.running(session_id)

    def running_background(session_id: str) -> int:
        mgr = STATE.background_mgr
        if mgr is None:
            return 0
        return len(mgr.store.list(origin_session=session_id, status="running"))

    def turn_running(session_id: str) -> bool:
        return turn_activity.is_active(session_id)

    return DriverSources(
        list_jobs=list_jobs,
        active_watches=active_watches,
        running_delegations=running_delegations,
        running_background=running_background,
        turn_running=turn_running,
    )


def _parse_dt(value: datetime | str | None) -> datetime | None:
    """A timezone-aware UTC datetime from a stored ISO string or a datetime; ``None``
    when it can't be read. Naive values are read as UTC (the store writes UTC)."""
    if isinstance(value, datetime):
        dt = value
    elif isinstance(value, str) and value.strip():
        try:
            dt = datetime.fromisoformat(value.strip())
        except ValueError:
            return None
    else:
        return None
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=UTC)


def _age_seconds(now: datetime | str, updated_at: datetime | str | None) -> float | None:
    """Seconds between ``updated_at`` and ``now``, or ``None`` if either is unreadable."""
    now_dt = _parse_dt(now)
    upd_dt = _parse_dt(updated_at)
    if now_dt is None or upd_dt is None:
        return None
    return (now_dt - upd_dt).total_seconds()
