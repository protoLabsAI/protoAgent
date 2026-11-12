"""Additive column migrations raise on a real failure (#4733 follow-up).

The stores used to treat every ``sqlite3.OperationalError`` from an ALTER as
"column already present", so a lock timeout during a real migration passed as
done and every later insert failed until restart.
"""

from __future__ import annotations

import sqlite3

import pytest

from observability.ledger_store import LedgerStore
from observability.prompt_snapshots import PromptSnapshotStore
from observability.sqlite_migrate import add_missing_columns
from observability.telemetry_store import TelemetryStore


class _FailingAlter:
    """Wraps a connection so ALTERs fail with a chosen OperationalError."""

    def __init__(self, db: sqlite3.Connection, message: str) -> None:
        self._db = db
        self._message = message
        self.alters: list[str] = []

    def execute(self, sql: str, *args):
        if sql.lstrip().upper().startswith("ALTER"):
            self.alters.append(sql)
            raise sqlite3.OperationalError(self._message)
        return self._db.execute(sql, *args)

    def __getattr__(self, name: str):
        return getattr(self._db, name)


def _cols(db: sqlite3.Connection, table: str) -> set[str]:
    return {r[1] for r in db.execute(f"PRAGMA table_info({table})").fetchall()}


def test_adds_only_missing_columns():
    db = sqlite3.connect(":memory:")
    db.execute("CREATE TABLE t (id INTEGER, a TEXT)")
    added = add_missing_columns(db, "t", (("a", "TEXT"), ("b", "TEXT"), ("c", "INTEGER DEFAULT 0")))
    assert added == ["b", "c"]
    assert _cols(db, "t") == {"id", "a", "b", "c"}
    # A second open is a no-op and issues no ALTER at all.
    wrapped = _FailingAlter(db, "database is locked")
    assert add_missing_columns(wrapped, "t", (("a", "TEXT"), ("b", "TEXT"))) == []
    assert wrapped.alters == []


def test_duplicate_column_race_is_tolerated():
    db = sqlite3.connect(":memory:")
    db.execute("CREATE TABLE t (id INTEGER)")
    # The PRAGMA saw the column missing, but a concurrent opener added it first.
    wrapped = _FailingAlter(db, "duplicate column name: b")
    assert add_missing_columns(wrapped, "t", (("b", "TEXT"),)) == []
    assert len(wrapped.alters) == 1


def test_other_operational_error_raises():
    db = sqlite3.connect(":memory:")
    db.execute("CREATE TABLE t (id INTEGER)")
    wrapped = _FailingAlter(db, "database is locked")
    with pytest.raises(sqlite3.OperationalError, match="database is locked"):
        add_missing_columns(wrapped, "t", (("b", "TEXT"),))


def test_telemetry_store_adds_column_to_old_db(tmp_path):
    path = tmp_path / "telemetry.db"
    TelemetryStore(str(path))
    db = sqlite3.connect(path)
    db.execute("ALTER TABLE turns DROP COLUMN error")
    db.commit()
    assert "error" not in _cols(db, "turns")
    db.close()

    TelemetryStore(str(path))
    db = sqlite3.connect(path)
    assert "error" in _cols(db, "turns")
    db.close()


@pytest.mark.parametrize(
    ("store_cls", "filename", "table", "column"),
    [
        (TelemetryStore, "telemetry.db", "turns", "error"),
        (LedgerStore, "ledger.db", "delegations", "nudges"),
        (PromptSnapshotStore, "prompts.db", "calls", "wire_text"),
    ],
)
def test_store_open_raises_when_migration_fails(tmp_path, monkeypatch, store_cls, filename, table, column):
    path = tmp_path / filename
    store_cls(str(path))
    db = sqlite3.connect(path)
    db.execute(f"ALTER TABLE {table} DROP COLUMN {column}")
    db.commit()
    db.close()

    real_connect = store_cls._connect

    def _locked_connect(self):
        return _FailingAlter(real_connect(self), "database is locked")

    monkeypatch.setattr(store_cls, "_connect", _locked_connect)
    with pytest.raises(sqlite3.OperationalError, match="database is locked"):
        store_cls(str(path))


# Scheduler and activity schema-init failures stay NON-fatal: their constructors
# log and survive (agent_init wraps construction in `except Exception`, so raising
# would run the agent scheduler-less until the next restart). The failed ALTER
# still never passes as "column already present" — the migration is simply not
# committed, reads/claims fail closed per query, and the next clean open
# completes it (ADR 0129).


def _drop_and_lock(monkeypatch, cls, path, table, column):
    db = sqlite3.connect(path)
    db.execute(f"ALTER TABLE {table} DROP COLUMN {column}")
    db.commit()
    db.close()
    real_connect = cls._connect

    def _locked_connect(self):
        return _FailingAlter(real_connect(self), "database is locked")

    monkeypatch.setattr(cls, "_connect", _locked_connect)


def test_scheduler_store_logs_and_survives_a_failed_migration(tmp_path, monkeypatch, caplog):
    from scheduler.local import LocalScheduler

    s = LocalScheduler("mig-test", invoke_url="", db_dir=tmp_path)
    _drop_and_lock(monkeypatch, LocalScheduler, s.path, "jobs", "fence")
    with caplog.at_level("ERROR"):
        LocalScheduler("mig-test", invoke_url="", db_dir=tmp_path)
    assert "schema init failed" in caplog.text and "database is locked" in caplog.text
    # The failure was not swallowed as "column already present": the column is
    # still missing, and the next open without the lock completes the migration.
    assert "fence" not in _cols(sqlite3.connect(s.path), "jobs")
    monkeypatch.undo()
    LocalScheduler("mig-test", invoke_url="", db_dir=tmp_path)
    assert "fence" in _cols(sqlite3.connect(s.path), "jobs")


def test_scheduler_store_adds_a_missing_column(tmp_path):
    from scheduler.local import LocalScheduler

    s = LocalScheduler("mig-test", invoke_url="", db_dir=tmp_path)
    db = sqlite3.connect(s.path)
    db.execute("ALTER TABLE jobs DROP COLUMN fence")
    db.commit()
    db.close()
    LocalScheduler("mig-test", invoke_url="", db_dir=tmp_path)
    assert "fence" in _cols(sqlite3.connect(s.path), "jobs")


def test_activity_store_logs_a_failed_migration(tmp_path, monkeypatch, caplog):
    from activity.store import ActivityLog

    path = tmp_path / "activity.db"
    ActivityLog(str(path))
    _drop_and_lock(monkeypatch, ActivityLog, path, "activity", "stimulus")
    with caplog.at_level("ERROR"):
        ActivityLog(str(path))
    assert "schema init failed" in caplog.text and "database is locked" in caplog.text
