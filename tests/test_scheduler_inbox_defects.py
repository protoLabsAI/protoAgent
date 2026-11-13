"""Three small robustness defects in the scheduler store init and the now-inbox watch (#5126).

Named for the behavior (not ``*_5126``) so it does not add to the banned
``issue_numbered_test_files`` burndown — the issue number stays here in the docstring,
per ``docs/dev/testing.md``.

1. ``LocalScheduler`` one-time init closes its SQLite connection on every path — a
   ``DatabaseError`` raised mid-init (e.g. a lock timeout during ALTER TABLE) must not
   leak the connection.
2. ``_acquire_jobs_lock`` returns ``None`` (not acquired) and logs a warning when the
   lock file cannot be opened, instead of letting an ``OSError`` escape startup.
3. A now-inbox watch FIFO-evicted from the bounded watch table is still restored to the
   pending queue when its turn later fails.
"""

from __future__ import annotations

import logging
import sqlite3
from types import SimpleNamespace

import pytest

import runtime.state as rs
import scheduler.local as sl
import server.a2a as a2a
from inbox.store import InboxStore
from scheduler.local import LocalScheduler


def _make_scheduler(tmp_path, **kw) -> LocalScheduler:
    return LocalScheduler(
        agent_name="gina-test", invoke_url="http://127.0.0.1:7870", db_dir=tmp_path, **kw
    )


# ── defect 1: init closes its connection even when a statement raises mid-init ────────────


def test_init_db_closes_its_connection_on_databaseerror(tmp_path, monkeypatch):
    """Force a ``DatabaseError`` while the adopt path holds an open connection and assert
    the connection was closed (a closed sqlite3 connection raises on use) and nothing
    escaped the constructor."""
    opened: list[sqlite3.Connection] = []
    real_connect = sl.LocalScheduler._connect

    def _recording_connect(self):
        conn = real_connect(self)
        opened.append(conn)
        return conn

    def _raise_mid_init(self, db):
        raise sqlite3.DatabaseError("simulated lock timeout during ALTER TABLE")

    monkeypatch.setattr(sl.LocalScheduler, "_connect", _recording_connect)
    monkeypatch.setattr(sl.LocalScheduler, "_apply_additive_schema", _raise_mid_init)

    sched = _make_scheduler(tmp_path)  # must not raise

    assert sched is not None
    assert opened, "init never opened a connection to test"
    for conn in opened:
        with pytest.raises(sqlite3.ProgrammingError):
            conn.execute("SELECT 1")  # raises only if the connection is closed


# ── defect 2: a lock file that cannot be opened is "not acquired", not a crash ────────────


def test_acquire_jobs_lock_returns_none_when_open_raises_oserror(tmp_path, monkeypatch, caplog):
    pytest.importorskip("fcntl")  # the open()-OSError path only exists in the POSIX branch
    missing = tmp_path / "no-such-dir" / "jobs.db"  # parent is absent → open(..'.lock') raises
    monkeypatch.setattr(sl, "_LOCKED_PATHS", set())

    with caplog.at_level(logging.WARNING, logger="scheduler.local"):
        result = sl._acquire_jobs_lock(missing)

    assert result is None
    assert str(missing) not in sl._LOCKED_PATHS  # a failed open must not mark the path owned
    assert "owner-lock file" in caplog.text
    assert str(missing) in caplog.text  # the lock path is named in the warning


# ── defect 3: an evicted now-watch still restores its item when the turn fails ────────────


def test_evicted_inbox_watch_still_restores_on_failure(tmp_path, monkeypatch):
    store = InboxStore(str(tmp_path / "inbox.db"))
    monkeypatch.setattr(rs.STATE, "inbox_store", store, raising=False)
    monkeypatch.setattr(a2a, "_INBOX_WATCH_MAX", 1)  # evict after a single newer watch
    for d in (a2a._INBOX_TURN_WATCH, a2a._INBOX_EVICTED_WATCH, a2a._INBOX_EARLY_TERMINALS):
        d.clear()

    victim = store.add("evicted now page", priority="now")
    store.mark_delivered([victim["id"]])  # fired + marked delivered, as the real path does

    a2a._watch_inbox_turn("task-victim", victim)  # registered
    a2a._watch_inbox_turn("task-newer", {"id": -1})  # a newer watch evicts task-victim

    assert "task-victim" not in a2a._INBOX_TURN_WATCH
    assert "task-victim" in a2a._INBOX_EVICTED_WATCH  # reachable, not dropped

    outcome = SimpleNamespace(origin="inbox", task_id="task-victim", state="failed")
    a2a._inbox_turn_ended(outcome)  # no running loop → restores inline

    [pending] = store.list(priority_floor="now")
    assert pending["id"] == victim["id"]
    assert pending["delivered_at"] is None  # restored to the pending queue
    assert "task-victim" not in a2a._INBOX_EARLY_TERMINALS  # not lost to the unconsumed map
