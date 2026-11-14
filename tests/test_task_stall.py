"""The driver test + stall finder and the task store's stall-nudge counter (ADR 0129 D9 / S11a).

``is_driven`` / ``find_stalled`` (tasks.stall) run against injected fake ``sources`` so the
point-in-time driver logic is exercised without a live runtime; ``default_sources`` is smoke
checked to be None-safe. ``TaskStore.record_stall_nudge`` and the ``update`` reset + guarded
migration are exercised against a real SQLite store in a tmp path.
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from tasks.stall import (
    STALL_NUDGE_CAP,
    STALL_SWEEP_INTERVAL_S,
    STALL_THRESHOLD_S,
    DriverSources,
    default_sources,
    find_stalled,
    is_driven,
)
from tasks.store import TaskStore

NOW = datetime(2026, 11, 14, 12, 0, 0, tzinfo=UTC)


def _sources(**overrides) -> DriverSources:
    """An inert ``DriverSources`` (nothing driving anything), with per-test overrides."""
    base = dict(
        list_jobs=lambda: [],
        active_watches=lambda: [],
        running_delegations=lambda _sid: 0,
        running_background=lambda _sid: 0,
        turn_running=lambda _sid: False,
    )
    base.update(overrides)
    return DriverSources(**base)


def _task(**fields) -> dict:
    base = {"id": "task-1", "status": "in_progress", "session_id": "sess-1", "links": "", "updated_at": NOW.isoformat()}
    base.update(fields)
    return base


# ── constants ─────────────────────────────────────────────────────────────────


def test_constants_are_the_adr_defaults():
    assert STALL_THRESHOLD_S == 1800
    assert STALL_NUDGE_CAP == 3
    assert STALL_SWEEP_INTERVAL_S == 300


# ── is_driven: links ────────────────────────────────────────────────────────────


def test_driven_by_unresolved_link():
    task = _task(links='[{"ref": "bd-9", "kind": "card", "state": "open", "reason": ""}]')
    assert is_driven(task, sources=_sources()) is True


def test_not_driven_by_resolved_link():
    # Every link done and no other signal → nothing is driving the task.
    task = _task(links='[{"ref": "bd-9", "kind": "card", "state": "done", "reason": "merged"}]')
    assert is_driven(task, sources=_sources()) is False


def test_empty_session_is_judged_on_links_only():
    # No session to match → the session-scoped sources can't apply; a live turn in some
    # OTHER session must not count. Resolved link + blank session → undriven.
    driven = _task(session_id="", links='[{"ref": "bd-9", "state": "open"}]')
    undriven = _task(session_id="", links='[{"ref": "bd-9", "state": "done"}]')
    sources = _sources(turn_running=lambda _sid: True)  # would drive if the session matched
    assert is_driven(driven, sources=sources) is True
    assert is_driven(undriven, sources=sources) is False


# ── is_driven: session-scoped sources ─────────────────────────────────────────────


def test_driven_by_pending_job_in_the_session():
    job = SimpleNamespace(id="wait:sess-1", context_id="sess-1", origin_session=None)
    assert is_driven(_task(), sources=_sources(list_jobs=lambda: [job])) is True


def test_driven_by_pending_job_via_origin_session():
    job = SimpleNamespace(id="job-9", context_id="job:abc", origin_session="sess-1")
    assert is_driven(_task(), sources=_sources(list_jobs=lambda: [job])) is True


def test_not_driven_by_job_in_a_different_session():
    job = SimpleNamespace(id="job-9", context_id="sess-OTHER", origin_session="sess-OTHER")
    assert is_driven(_task(), sources=_sources(list_jobs=lambda: [job])) is False


def test_driven_by_active_watch_in_the_session():
    watch = SimpleNamespace(id="w1", run_session="sess-1")
    assert is_driven(_task(), sources=_sources(active_watches=lambda: [watch])) is True


def test_driven_by_running_delegation():
    assert is_driven(_task(), sources=_sources(running_delegations=lambda _sid: 2)) is True


def test_driven_by_running_background_job():
    assert is_driven(_task(), sources=_sources(running_background=lambda _sid: 1)) is True


def test_driven_by_running_turn():
    assert is_driven(_task(), sources=_sources(turn_running=lambda _sid: True)) is True


def test_undriven_when_no_signal():
    assert is_driven(_task(), sources=_sources()) is False


# ── is_driven: fail-quiet ─────────────────────────────────────────────────────────


def test_raising_source_counts_as_driven():
    def boom():
        raise RuntimeError("scheduler DB unreadable")

    # A source we can't read is never evidence of a stall — treat the task as driven.
    assert is_driven(_task(), sources=_sources(list_jobs=boom)) is True


def test_raising_later_source_counts_as_driven():
    def boom(_sid):
        raise RuntimeError("background store unreadable")

    assert is_driven(_task(), sources=_sources(running_background=boom)) is True


# ── find_stalled ─────────────────────────────────────────────────────────────────


def test_undriven_task_is_stalled_at_31_minutes_not_29():
    at_31 = _task(id="task-a", updated_at=(NOW - timedelta(minutes=31)).isoformat())
    at_29 = _task(id="task-b", updated_at=(NOW - timedelta(minutes=29)).isoformat())
    assert [t["id"] for t in find_stalled([at_31], now=NOW, sources=_sources())] == ["task-a"]
    assert find_stalled([at_29], now=NOW, sources=_sources()) == []


def test_driven_old_task_is_not_stalled():
    old = _task(updated_at=(NOW - timedelta(hours=5)).isoformat())
    assert find_stalled([old], now=NOW, sources=_sources(turn_running=lambda _sid: True)) == []


@pytest.mark.parametrize("status", ["open", "blocked", "waiting", "deferred", "closed"])
def test_non_in_progress_statuses_are_never_stalled(status):
    old = _task(status=status, updated_at=(NOW - timedelta(hours=5)).isoformat())
    assert find_stalled([old], now=NOW, sources=_sources()) == []


def test_unparseable_updated_at_is_skipped():
    # Can't prove it's old → don't nudge.
    assert find_stalled([_task(updated_at="")], now=NOW, sources=_sources()) == []
    assert find_stalled([_task(updated_at="not-a-date")], now=NOW, sources=_sources()) == []


# ── default_sources (live wiring) ─────────────────────────────────────────────────


def test_default_sources_is_none_safe(monkeypatch):
    # With no scheduler/watch/background wired and a session nothing else touched, the
    # live sources report no evidence and never raise — an undriven task reads undriven.
    monkeypatch.setattr("runtime.state.STATE.scheduler", None)
    monkeypatch.setattr("runtime.state.STATE.watch_controller", None)
    monkeypatch.setattr("runtime.state.STATE.background_mgr", None)
    sources = default_sources()
    assert sources.list_jobs() == []
    assert sources.active_watches() == []
    assert is_driven(_task(session_id="stall-test-unique-session"), sources=sources) is False


# ── store: stall_nudges column ────────────────────────────────────────────────────


@pytest.fixture
def store(tmp_path):
    return TaskStore(db_path=str(tmp_path / "issues.db"))


def test_new_issue_exposes_zero_stall_nudges(store):
    i = store.create("t")
    assert i["stall_nudges"] == 0
    assert store.get(i["id"])["stall_nudges"] == 0


def test_record_stall_nudge_increments_without_touching_updated_at(store):
    i = store.create("t")
    before = store.get(i["id"])
    assert store.record_stall_nudge(i["id"]) == 1
    assert store.record_stall_nudge(i["id"]) == 2
    after = store.get(i["id"])
    assert after["stall_nudges"] == 2
    assert after["updated_at"] == before["updated_at"]


def test_record_stall_nudge_unknown_issue_raises(store):
    with pytest.raises(KeyError):
        store.record_stall_nudge("task-404")


def test_normal_update_resets_stall_nudges(store):
    i = store.create("t")
    store.record_stall_nudge(i["id"])
    store.record_stall_nudge(i["id"])
    assert store.get(i["id"])["stall_nudges"] == 2
    out = store.update(i["id"], status="in_progress")
    assert out["stall_nudges"] == 0


def test_existing_db_without_the_column_migrates(tmp_path):
    path = tmp_path / "issues.db"
    # An old-schema board written before the stall_nudges column existed.
    conn = sqlite3.connect(str(path))
    conn.execute(
        """
        CREATE TABLE issues (
            id TEXT PRIMARY KEY, title TEXT NOT NULL, description TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL DEFAULT 'open', priority INTEGER NOT NULL DEFAULT 2,
            issue_type TEXT NOT NULL DEFAULT 'task', assignee TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL, updated_at TEXT NOT NULL, closed_at TEXT, close_reason TEXT,
            session_id TEXT NOT NULL DEFAULT '', ask TEXT NOT NULL DEFAULT '', links TEXT NOT NULL DEFAULT ''
        )
        """
    )
    ts = NOW.isoformat()
    conn.execute(
        "INSERT INTO issues (id, title, status, created_at, updated_at, session_id) VALUES (?,?,?,?,?,?)",
        ("task-5", "old task", "in_progress", ts, ts, "sess-1"),
    )
    conn.commit()
    conn.close()

    store = TaskStore(db_path=str(path))
    row = store.get("task-5")
    assert row is not None
    assert row["stall_nudges"] == 0  # existing row got the non-destructive default
    assert row["updated_at"] == ts  # migration didn't rewrite the row
    assert store.record_stall_nudge("task-5") == 1
