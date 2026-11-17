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


# ── is_driven: links ────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "links, expected",
    [
        pytest.param('[{"ref": "bd-9", "kind": "card", "state": "open", "reason": ""}]', True, id="unresolved-link"),
        pytest.param('[{"ref": "bd-9", "kind": "card", "state": "done", "reason": "merged"}]', False, id="resolved-link"),
    ],
)
def test_links_drive_until_resolved(links, expected):
    # A link is durable evidence until its state is done; resolved + no other signal → undriven.
    assert is_driven(_task(links=links), sources=_sources()) is expected


@pytest.mark.parametrize(
    "links",
    [
        pytest.param('[{"ref": "https://example.com/notes", "kind": "url", "state": ""}]', id="url-only"),
        pytest.param(
            '[{"ref": "conv:codex/chat-1", "kind": "conversation", "roles": ["coder"],'
            ' "owner": "codex", "context_id": "chat-1", "state": "", "reason": ""}]',
            id="conversation-only",
        ),
    ],
)
def test_informational_links_never_drive_a_task(links):
    # Plain URLs and related-conversation links (P2b) carry no work to finish; a task
    # whose only links are informational has nothing driving it, so with no live signal
    # it reads as undriven (and so can be found stalled).
    assert is_driven(_task(links=links), sources=_sources()) is False


def test_empty_session_is_judged_on_links_only():
    # No session to match → the session-scoped sources can't apply; a live turn in some
    # OTHER session must not count. Resolved link + blank session → undriven.
    driven = _task(session_id="", links='[{"ref": "bd-9", "state": "open"}]')
    undriven = _task(session_id="", links='[{"ref": "bd-9", "state": "done"}]')
    sources = _sources(turn_running=lambda _sid: True)  # would drive if the session matched
    assert is_driven(driven, sources=sources) is True
    assert is_driven(undriven, sources=sources) is False


# ── is_driven: session-scoped sources ─────────────────────────────────────────────

# One table, not one function per variant (docs/dev/testing.md): each case is the SAME
# driver test with a different live signal injected. The task is in session "sess-1".
_JOB_IN_SESSION = SimpleNamespace(id="wait:sess-1", context_id="sess-1", origin_session=None)
_JOB_VIA_ORIGIN = SimpleNamespace(id="job-9", context_id="job:abc", origin_session="sess-1")
_JOB_OTHER_SESSION = SimpleNamespace(id="job-9", context_id="sess-OTHER", origin_session="sess-OTHER")
_WATCH_IN_SESSION = SimpleNamespace(id="w1", run_session="sess-1")
_WATCH_OTHER_SESSION = SimpleNamespace(id="w1", run_session="sess-OTHER")


@pytest.mark.parametrize(
    "overrides, expected",
    [
        pytest.param({"list_jobs": lambda: [_JOB_IN_SESSION]}, True, id="pending-job-by-context"),
        pytest.param({"list_jobs": lambda: [_JOB_VIA_ORIGIN]}, True, id="pending-job-by-origin"),
        pytest.param({"list_jobs": lambda: [_JOB_OTHER_SESSION]}, False, id="job-in-other-session"),
        pytest.param({"active_watches": lambda: [_WATCH_IN_SESSION]}, True, id="active-watch"),
        pytest.param({"active_watches": lambda: [_WATCH_OTHER_SESSION]}, False, id="watch-in-other-session"),
        pytest.param({"running_delegations": lambda _sid: 2}, True, id="running-delegation"),
        pytest.param({"running_background": lambda _sid: 1}, True, id="running-background"),
        pytest.param({"turn_running": lambda _sid: True}, True, id="running-turn"),
        pytest.param({}, False, id="no-signal"),
    ],
)
def test_session_scoped_sources_drive_the_task(overrides, expected):
    assert is_driven(_task(), sources=_sources(**overrides)) is expected


# ── is_driven: fail-quiet ─────────────────────────────────────────────────────────


def _raise_no_arg():
    raise RuntimeError("scheduler DB unreadable")


def _raise_one_arg(_sid):
    raise RuntimeError("background store unreadable")


@pytest.mark.parametrize(
    "overrides",
    [
        pytest.param({"list_jobs": _raise_no_arg}, id="first-source-raises"),
        pytest.param({"running_background": _raise_one_arg}, id="later-source-raises"),
    ],
)
def test_raising_source_counts_as_driven(overrides):
    # A source we can't read is never evidence of a stall — treat the task as driven.
    assert is_driven(_task(), sources=_sources(**overrides)) is True


# ── find_stalled ─────────────────────────────────────────────────────────────────


def test_undriven_task_is_stalled_at_31_minutes_not_29():
    at_31 = _task(id="task-a", updated_at=(NOW - timedelta(minutes=31)).isoformat())
    at_29 = _task(id="task-b", updated_at=(NOW - timedelta(minutes=29)).isoformat())
    assert [t["id"] for t in find_stalled([at_31], now=NOW, sources=_sources())] == ["task-a"]
    assert find_stalled([at_29], now=NOW, sources=_sources()) == []


def test_driven_old_task_is_not_stalled():
    old = _task(updated_at=(NOW - timedelta(hours=5)).isoformat())
    assert find_stalled([old], now=NOW, sources=_sources(turn_running=lambda _sid: True)) == []


def test_conversation_only_old_task_is_stalled():
    # A task whose only links are related conversations (informational, state "") has
    # nothing driving it: left untouched past the threshold it must be found stalled,
    # not shielded by the provenance link.
    links = (
        '[{"ref": "conv:codex/chat-1", "kind": "conversation", "roles": ["coder"],'
        ' "owner": "codex", "context_id": "chat-1", "state": "", "reason": ""}]'
    )
    old = _task(updated_at=(NOW - timedelta(hours=5)).isoformat(), links=links)
    assert [t["id"] for t in find_stalled([old], now=NOW, sources=_sources())] == ["task-1"]


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


def test_live_list_jobs_source_raises_on_scheduler_db_error(monkeypatch):
    # A scheduler DB hiccup must RAISE through the live source (→ is_driven treats the
    # task as driven), never read as the swallowed empty list list_jobs() returns on a
    # hiccup — that would look like "no pending job" and could nudge a driven task.
    def boom() -> list:
        raise sqlite3.DatabaseError("database disk image is malformed")

    sched = SimpleNamespace(list_jobs_strict=boom, list_jobs=lambda: [])
    monkeypatch.setattr("runtime.state.STATE.scheduler", sched)
    sources = default_sources()
    with pytest.raises(sqlite3.DatabaseError):
        sources.list_jobs()
    assert is_driven(_task(), sources=sources) is True


# ── store: stall_nudges column ────────────────────────────────────────────────────


@pytest.fixture
def store(tmp_path):
    return TaskStore(db_path=str(tmp_path / "issues.db"))


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
