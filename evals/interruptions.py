"""Interruption counter — how often unattended work reached the operator in 24 hours (#5518).

For a 24-hour window ending at a given time this counts, per source (``scheduler``,
``inbox``, ``watch``, ``background``), (a) the **Activity cards** posted and (b) the
**Needs-you items** raised. It only counts; thresholds and the nightly wiring are a
later card, and nothing here changes how a card or a Needs-you item is posted.

The record each count is read from
----------------------------------

Every count comes from a durable row in one of the instance's own stores
(``instance_paths().store(...)``):

Activity cards

* ``scheduler``, ``inbox``, ``watch`` — the **Activity log**: the ``activity`` table in
  ``activity/agent.db`` (``activity/store.py``), one row per unattended turn that
  reached the Activity feed, attributed by its ``origin`` column and timed by
  ``created_at``.
* ``scheduler``, ``watch`` — the **scheduler run history**: the ``occurrences`` table in
  ``scheduler/agent/jobs.db`` (``scheduler/local.py``), through its ``reported`` flag
  (#5543). A run is one card however many surfaces showed it, joined on ``task_id``:

  - ``reported = 0`` — the run ended with ``NO_UPDATE`` or repeated its last result, so
    no card was posted. Its Activity log row is NOT counted.
  - ``reported = 1`` — the result card was posted to the chat that owns the job. It is
    counted once: by its Activity log row when there is one, else by this row's
    ``ended_at``.
  - ``reported`` unknown (a failed or cancelled run, or a job with no chat to report
    to) — counted by its Activity log row alone.

  A job id starting ``watch-`` is a watch reaction; every other job is ``scheduler``.
* ``background`` — the **background job ledger**: the ``background_jobs`` table in
  ``background/agent.db`` (``background/store.py``). Each job settled in the window
  (``completed_at``) posted one completion card. Activity log rows with a background
  origin are the briefing of an already-counted completion, so they are not counted.

Needs-you items

* ``inbox`` — the **inbox**: the ``inbox`` table in ``inbox/agent.db``
  (``inbox/store.py``). A row raises Needs-you when it is operator-class
  (``needs_human``, or a known operator-class producer) or its automatic delivery
  retries are spent; it is timed by ``created_at``.
* every source — the **decisions ledger**: the ``decisions`` table in
  ``decisions/decisions.db`` (``graph/decisions.py``), one row per parked
  ``request_decision``, timed by ``created_at``. The row names a session, not a source,
  so the source comes from whichever durable record owns that session: a
  restart-recovery marker naming a job, a ``background:`` session, the scheduler's
  ``job_sessions`` table, the inbox's ``inbox_executions`` table, then the Activity
  log's ``context_id``. A decision raised in an operator's own chat belongs to no
  unattended source and is not counted.

Not counted: a task parked ``waiting`` and a stopped goal. Their rows record neither
when they were raised nor which source raised them.

Shape
-----

:func:`count_interruptions` is the pure core: it takes the records as plain mappings and
touches no store, so tests need no live instance. :func:`load_records` is the thin
reader: it opens the stores of the instance THIS process runs under (the same rule as
the nightly's friction ledger — see ``scripts/evals_nightly.py``), read-only, never a
throwaway sweep instance. A store that is missing or predates a column reads as no
records. Only the default per-instance stores are read; a store relocated by
``inbox_db_path`` or ``SCHEDULER_DB_DIR`` is not followed.

Usage::

    python -m evals.interruptions                       # the last 24 hours, as JSON
    PROTOAGENT_INSTANCE=dev python -m evals.interruptions --end 2026-11-22T06:00:00+00:00
"""

from __future__ import annotations

import argparse
import json
import sqlite3
from collections.abc import Iterable, Mapping
from datetime import UTC, datetime, timedelta
from pathlib import Path

SOURCES = ("scheduler", "inbox", "watch", "background")
WINDOW = timedelta(hours=24)

#: Activity log ``origin`` → source. A background origin maps to ``background`` only to
#: attribute a decision's session; those rows are never counted as cards themselves.
_ORIGIN_SOURCE = {
    "scheduler": "scheduler",
    "inbox": "inbox",
    "watch": "watch",
    "background": "background",
    "background-resume": "background",
}
_WATCH_JOB_PREFIX = "watch-"
_BACKGROUND_SESSION_PREFIX = "background:"
_RESTART_RECOVERY_PREFIX = "restart-recovery:"
_ACTIVITY_CONTEXT = "system:activity"


def _parse_time(value: object) -> datetime | None:
    """A stored timestamp as an aware UTC datetime, or ``None`` when it isn't one.
    A naive value is read as UTC, the zone every store writes."""
    if isinstance(value, datetime):
        stamp = value
    elif isinstance(value, bool) or value is None:
        return None
    elif isinstance(value, (int, float)):
        try:
            stamp = datetime.fromtimestamp(value, UTC)
        except (OverflowError, OSError, ValueError):
            return None
    else:
        text = str(value).strip()
        if not text:
            return None
        try:
            stamp = datetime.fromisoformat(text.replace("Z", "+00:00") if text.endswith("Z") else text)
        except ValueError:
            return None
    return stamp.replace(tzinfo=UTC) if stamp.tzinfo is None else stamp.astimezone(UTC)


def _job_source(job_id: object) -> str:
    return "watch" if str(job_id or "").startswith(_WATCH_JOB_PREFIX) else "scheduler"


def _text(row: Mapping, key: str) -> str:
    return str(row.get(key) or "").strip()


def _restart_recovery_job(interrupt_id: str) -> str:
    """The job id a restart-recovery decision names, or ``""``."""
    if not interrupt_id.startswith(_RESTART_RECOVERY_PREFIX):
        return ""
    try:
        ref = json.loads(interrupt_id[len(_RESTART_RECOVERY_PREFIX):])
    except ValueError:
        return ""
    return str(ref.get("job_id") or "") if isinstance(ref, dict) else ""


def count_interruptions(
    *,
    window_end: datetime,
    activity: Iterable[Mapping] = (),
    scheduler_runs: Iterable[Mapping] = (),
    background_jobs: Iterable[Mapping] = (),
    inbox_items: Iterable[Mapping] = (),
    decisions: Iterable[Mapping] = (),
    job_sessions: Iterable[Mapping] = (),
    inbox_executions: Iterable[Mapping] = (),
    window: timedelta = WINDOW,
) -> dict[str, dict[str, int]]:
    """Per-source ``{"activity_cards": n, "needs_you": n}`` for the window ending at
    ``window_end`` (start exclusive, end inclusive). Pure: every argument is a sequence
    of row mappings shaped like the store tables the module docstring names.

    Every source is always present, at 0 when it has no records. A row whose timestamp is
    missing, unreadable or outside the window is not counted."""
    end = _parse_time(window_end)
    if end is None:
        raise ValueError("window_end must be a datetime")
    start = end - window

    def in_window(value: object) -> bool:
        stamp = _parse_time(value)
        return stamp is not None and start < stamp <= end

    counts = {source: {"activity_cards": 0, "needs_you": 0} for source in SOURCES}
    activity = list(activity)
    scheduler_runs = list(scheduler_runs)

    # ── Activity cards ────────────────────────────────────────────────────────────
    quiet_runs = {
        _text(run, "task_id") for run in scheduler_runs
        if run.get("reported") is not None and not run.get("reported") and _text(run, "task_id")
    }
    fed_runs: set[str] = set()  # runs the Activity log already carries, in any window
    for row in activity:
        source = _ORIGIN_SOURCE.get(_text(row, "origin"))
        if source not in ("scheduler", "inbox", "watch"):
            continue
        task_id = _text(row, "task_id")
        if source != "inbox" and task_id:
            fed_runs.add(task_id)
            if task_id in quiet_runs:
                continue
        if in_window(row.get("created_at")):
            counts[source]["activity_cards"] += 1
    for run in scheduler_runs:
        if not run.get("reported") or _text(run, "task_id") in fed_runs:
            continue
        if in_window(run.get("ended_at") or run.get("started_at")):
            counts[_job_source(run.get("job_id"))]["activity_cards"] += 1
    for job in background_jobs:
        if in_window(job.get("completed_at")):
            counts["background"]["activity_cards"] += 1

    # ── Needs-you items ───────────────────────────────────────────────────────────
    for item in inbox_items:
        if (item.get("needs_human") or item.get("retries_exhausted")) and in_window(item.get("created_at")):
            counts["inbox"]["needs_you"] += 1

    session_source: dict[str, str] = {}
    # Lowest precedence first, so a later (more specific) record overwrites.
    for row in activity:
        context = _text(row, "context_id")
        source = _ORIGIN_SOURCE.get(_text(row, "origin"))
        if context and context != _ACTIVITY_CONTEXT and source:
            session_source[context] = source
    for row in inbox_executions:
        if _text(row, "context_id"):
            session_source[_text(row, "context_id")] = "inbox"
    for row in job_sessions:
        if _text(row, "context_id"):
            session_source[_text(row, "context_id")] = _job_source(row.get("job_id"))

    for decision in decisions:
        if not in_window(decision.get("created_at")):
            continue
        session = _text(decision, "session_id")
        job_id = _restart_recovery_job(_text(decision, "interrupt_id"))
        if job_id:
            source = _job_source(job_id)
        elif session.startswith(_BACKGROUND_SESSION_PREFIX):
            source = "background"
        else:
            source = session_source.get(session, "")
        if source:
            counts[source]["needs_you"] += 1
    return counts


# ── the thin reader ───────────────────────────────────────────────────────────────


def _agent_db(base: Path, filename: str = "agent.db") -> Path | None:
    """One of this instance's per-agent stores, read-only: the constant-name file, else
    the single name-keyed file an older install left (the rule ``server.stores`` and
    ``scheduler.local`` apply). ``None`` when there is no store, or no unambiguous one."""
    target = base / filename
    if target.is_file():
        return target
    if not base.is_dir():
        return None
    legacy = sorted(p for p in base.glob("*.db") if p.name != filename)
    return legacy[0] if len(legacy) == 1 else None


def _scheduler_db(root: Path) -> Path | None:
    target = root / "agent" / "jobs.db"
    if target.is_file():
        return target
    if not root.is_dir():
        return None
    legacy = sorted(d / "jobs.db" for d in root.iterdir() if d.name != "agent" and (d / "jobs.db").is_file())
    return legacy[0] if len(legacy) == 1 else None


def _rows(path: Path | None, table: str, columns: tuple[str, ...]) -> list[dict]:
    """``columns`` of every row in ``table``, opened read-only. A missing store, table or
    column reads as absent — the counter never creates or migrates a store."""
    if path is None or not path.is_file():
        return []
    try:
        db = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True, timeout=5)
    except sqlite3.Error:
        return []
    try:
        db.row_factory = sqlite3.Row
        present = {r["name"] for r in db.execute(f"PRAGMA table_info({table})")}
        wanted = [c for c in columns if c in present]
        if not wanted:
            return []
        return [dict(r) for r in db.execute(f"SELECT {', '.join(wanted)} FROM {table}")]
    except sqlite3.Error:
        return []
    finally:
        db.close()


def load_records() -> dict[str, list[dict]]:
    """The records :func:`count_interruptions` takes, read from the stores of the
    instance this process runs under (``instance_paths()``). Read-only."""
    from inbox.store import RECOVERY_MAX_ATTEMPTS, needs_operator_attention
    from infra.paths import instance_paths

    paths = instance_paths()
    inbox_db = _agent_db(paths.store("inbox"))
    scheduler_db = _scheduler_db(paths.store("scheduler"))
    inbox_items = _rows(
        inbox_db, "inbox",
        ("id", "created_at", "priority", "source", "delivered_at", "needs_human",
         "recovery_attempts", "recovery_accepted_at"),
    )
    for item in inbox_items:
        # The same derivation /api/inbox applies (operator_api.console_handlers).
        item["needs_human"] = needs_operator_attention(item)
        item["retries_exhausted"] = bool(
            not item.get("delivered_at")
            and item.get("priority") == "now"
            and not item.get("recovery_accepted_at")
            and int(item.get("recovery_attempts") or 0) >= RECOVERY_MAX_ATTEMPTS
        )
    return {
        "activity": _rows(
            _agent_db(paths.store("activity")), "activity", ("created_at", "context_id", "origin", "task_id")
        ),
        "scheduler_runs": _rows(
            scheduler_db, "occurrences", ("job_id", "task_id", "started_at", "ended_at", "reported")
        ),
        "background_jobs": _rows(
            _agent_db(paths.store("background")), "background_jobs", ("id", "status", "completed_at")
        ),
        "inbox_items": inbox_items,
        "decisions": _rows(
            paths.store("decisions") / "decisions.db", "decisions",
            ("id", "session_id", "interrupt_id", "created_at"),
        ),
        "job_sessions": _rows(scheduler_db, "job_sessions", ("job_id", "context_id")),
        "inbox_executions": _rows(inbox_db, "inbox_executions", ("item_id", "context_id")),
    }


def read_interruptions(window_end: datetime | None = None) -> dict[str, dict[str, int]]:
    """Count this instance's last 24 hours, ending at ``window_end`` (default: now)."""
    return count_interruptions(window_end=window_end or datetime.now(UTC), **load_records())


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--end", help="window end as an ISO-8601 time (default: now, UTC)")
    args = parser.parse_args(argv)
    end = _parse_time(args.end) if args.end else datetime.now(UTC)
    if end is None:
        parser.error(f"--end is not an ISO-8601 time: {args.end!r}")
    print(json.dumps({"window_end": end.isoformat(), "sources": read_interruptions(end)}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
