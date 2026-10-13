"""The one-time legacy auto_vacuum migration of the checkpoint DB (graph/checkpoint_migrate.py).

A DB created before the checkpointer set ``auto_vacuum=INCREMENTAL`` is NONE: pruning
frees its rows, but the file never shrinks (34 GB holding 85 MB live, on a real machine).
Boot migrates it before the checkpointer opens it, when that's worth it and safe.
"""

from __future__ import annotations

import logging
import os
import sqlite3
import subprocess
import sys
from collections import namedtuple
from pathlib import Path

import pytest

from graph import checkpoint_migrate
from graph.checkpoint_migrate import migrate_legacy_auto_vacuum, run_checkpoint_cli
from graph.checkpointer import build_sqlite_checkpointer

_Usage = namedtuple("_Usage", "total used free")


def _legacy_db(path: Path, *, rows: int = 4000, keep: int = 200, mode: str = "NONE") -> Path:
    """A WAL checkpoint-shaped DB in ``auto_vacuum=<mode>``, filled, then mostly deleted."""
    conn = sqlite3.connect(path)
    conn.execute(f"PRAGMA auto_vacuum={mode}")
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute(
        "CREATE TABLE checkpoints (thread_id TEXT, checkpoint_ns TEXT, checkpoint_id TEXT, "
        "parent_checkpoint_id TEXT, type TEXT, checkpoint BLOB, metadata BLOB, "
        "PRIMARY KEY (thread_id, checkpoint_ns, checkpoint_id))"
    )
    conn.executemany(
        "INSERT INTO checkpoints VALUES (?,?,?,?,?,?,?)",
        [(f"t{i % 7}", "", f"c{i:06d}", None, "msgpack", os.urandom(2048), b"{}") for i in range(rows)],
    )
    conn.commit()
    conn.execute("DELETE FROM checkpoints WHERE rowid > ?", (keep,))
    conn.commit()
    conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    conn.close()
    return path


def _pragma(db: Path, name: str):
    conn = sqlite3.connect(db)
    try:
        return conn.execute(f"PRAGMA {name}").fetchone()[0]
    finally:
        conn.close()


def _rows(db: Path) -> list[tuple]:
    conn = sqlite3.connect(db)
    try:
        return conn.execute("SELECT thread_id, checkpoint_id, checkpoint FROM checkpoints ORDER BY checkpoint_id").fetchall()
    finally:
        conn.close()


@pytest.mark.platform_sensitive
def test_legacy_bloated_db_is_migrated_shrunk_and_intact(tmp_path, caplog):
    db = _legacy_db(tmp_path / "checkpoints.db")
    assert _pragma(db, "auto_vacuum") == 0
    before_rows = _rows(db)
    before_size = db.stat().st_size

    with caplog.at_level(logging.INFO, logger="protoagent.checkpoint_migrate"):
        res = migrate_legacy_auto_vacuum(db)

    assert res["status"] == "migrated", res
    assert _pragma(db, "auto_vacuum") == 2
    assert db.stat().st_size < before_size / 5
    assert res["size_before"] >= before_size and res["size_after"] < res["size_before"]
    assert _rows(db) == before_rows
    assert _pragma(db, "integrity_check") == "ok"
    # Before and after sizes are both logged.
    assert "migrating" in caplog.text and "migrated" in caplog.text and "->" in caplog.text
    # The checkpointer opens it normally afterwards.
    saver = build_sqlite_checkpointer(str(db))
    saver.conn.close()
    assert _rows(db) == before_rows


def test_boot_path_migrates_before_the_checkpointer_opens(tmp_path, monkeypatch):
    """``server.stores._build_checkpointer``, the single place the server builds the
    checkpointer, runs the migration first, and the saver it returns sees every row."""
    from server import stores

    db = _legacy_db(tmp_path / "checkpoints.db")
    before_rows = _rows(db)
    before_size = db.stat().st_size
    monkeypatch.setattr(stores, "_resolve_checkpoint_db", lambda configured: str(db))

    class _Cfg:
        checkpoint_db_path = "/sandbox/checkpoints.db"
        checkpoint_vacuum_legacy_on_start = True

    saver = stores._build_checkpointer(_Cfg())
    try:
        assert type(saver).__name__ == "ThreadedSqliteSaver"
        assert saver.conn.execute("PRAGMA auto_vacuum").fetchone()[0] == 2
        assert saver.conn.execute("SELECT COUNT(*) FROM checkpoints").fetchone()[0] == len(before_rows)
    finally:
        saver.conn.close()
    assert db.stat().st_size < before_size / 5


def test_boot_path_knob_off_leaves_the_legacy_db_alone(tmp_path, monkeypatch):
    from server import stores

    db = _legacy_db(tmp_path / "checkpoints.db")
    monkeypatch.setattr(stores, "_resolve_checkpoint_db", lambda configured: str(db))
    called = []
    monkeypatch.setattr(checkpoint_migrate, "migrate_legacy_auto_vacuum", lambda *a, **k: called.append(a))

    class _Cfg:
        checkpoint_db_path = "/sandbox/checkpoints.db"
        checkpoint_vacuum_legacy_on_start = False

    stores._build_checkpointer(_Cfg()).conn.close()
    assert called == []
    assert _pragma(db, "auto_vacuum") == 0


def test_healthy_incremental_db_is_untouched(tmp_path, monkeypatch):
    db = tmp_path / "checkpoints.db"
    build_sqlite_checkpointer(str(db)).conn.close()  # a new DB is created INCREMENTAL
    assert _pragma(db, "auto_vacuum") == 2
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE t (b BLOB)")
    conn.executemany("INSERT INTO t VALUES (?)", [(os.urandom(2048),)] * 500)
    conn.commit()
    conn.execute("DELETE FROM t")
    conn.commit()
    conn.close()
    mtime, size = db.stat().st_mtime_ns, db.stat().st_size

    def _no_disk_check(*a, **k):  # never reached: a healthy DB stops before the disk check
        raise AssertionError("healthy DB should not be considered for VACUUM")

    monkeypatch.setattr(checkpoint_migrate, "_disk_shortfall", _no_disk_check)
    res = migrate_legacy_auto_vacuum(db)
    assert res["status"] == "healthy"
    assert (db.stat().st_mtime_ns, db.stat().st_size) == (mtime, size)


def test_new_db_is_still_created_incremental(tmp_path):
    """Regression: the checkpointer set journal_mode=WAL BEFORE auto_vacuum, and switching
    to WAL writes the header, so the auto_vacuum pragma was a silent no-op and every DB it
    created was NONE (the root cause of the unbounded files)."""
    db = tmp_path / "fresh.db"
    assert migrate_legacy_auto_vacuum(db)["status"] == "missing"
    assert not db.exists()
    build_sqlite_checkpointer(str(db)).conn.close()
    assert _pragma(db, "auto_vacuum") == 2


def test_low_freelist_legacy_db_is_skipped(tmp_path):
    db = _legacy_db(tmp_path / "checkpoints.db", rows=1000, keep=900)  # ~10% free, well under 256 MB
    size = db.stat().st_size
    res = migrate_legacy_auto_vacuum(db)
    assert res["status"] == "not_worth_it", res
    assert _pragma(db, "auto_vacuum") == 0
    assert db.stat().st_size == size
    # --force (the CLI's escape hatch) skips the thresholds.
    assert migrate_legacy_auto_vacuum(db, force=True)["status"] == "migrated"
    assert _pragma(db, "auto_vacuum") == 2


def test_free_bytes_threshold_alone_triggers(tmp_path):
    """A big DB with a small freelist FRACTION but lots of free BYTES is still worth it."""
    db = _legacy_db(tmp_path / "checkpoints.db", rows=1000, keep=900)
    res = migrate_legacy_auto_vacuum(db, min_free_bytes=64 * 1024)
    assert res["status"] == "migrated", res


def test_low_disk_is_skipped(tmp_path, monkeypatch, caplog):
    db = _legacy_db(tmp_path / "checkpoints.db")
    size = db.stat().st_size
    monkeypatch.setattr(checkpoint_migrate.shutil, "disk_usage", lambda p: _Usage(10**12, 10**12 - 1024, 1024))
    with caplog.at_level(logging.WARNING, logger="protoagent.checkpoint_migrate"):
        res = migrate_legacy_auto_vacuum(db)
    assert res["status"] == "low_disk", res
    assert "NOT migrating" in caplog.text
    assert _pragma(db, "auto_vacuum") == 0
    assert db.stat().st_size == size


def test_low_disk_in_a_separate_temp_dir_is_skipped(tmp_path, monkeypatch):
    """The temp copy lands in SQLite's temp dir. When that's another volume, it is
    checked too."""
    db = _legacy_db(tmp_path / "checkpoints.db")
    tmp_dir = tmp_path / "sqlite-tmp"
    tmp_dir.mkdir()
    monkeypatch.setenv("SQLITE_TMPDIR", str(tmp_dir))
    real_stat = os.stat

    class _St:
        def __init__(self, st, dev):
            self._st, self.st_dev = st, dev

        def __getattr__(self, name):
            return getattr(self._st, name)

    def _stat(p, *a, **k):
        st = real_stat(p, *a, **k)
        return _St(st, 999) if str(p) == str(tmp_dir) else st

    monkeypatch.setattr(checkpoint_migrate.os, "stat", _stat)
    monkeypatch.setattr(
        checkpoint_migrate.shutil,
        "disk_usage",
        lambda p: _Usage(10**12, 0, 1024 if str(p) == str(tmp_dir) else 10**12),
    )
    res = migrate_legacy_auto_vacuum(db)
    assert res["status"] == "low_disk" and "temp dir" in res["reason"], res
    assert _pragma(db, "auto_vacuum") == 0


# Exclusivity rests on SQLite's locks, which the Windows VFS implements differently
# (LockFileEx vs POSIX advisory locks): run these on the Windows lane too.
@pytest.mark.platform_sensitive
def test_db_open_in_another_process_is_skipped(tmp_path):
    """Another process holding the DB (e.g. a second server for this instance) makes the
    migration skip instead of VACUUMing under it."""
    db = _legacy_db(tmp_path / "checkpoints.db")
    holder = subprocess.Popen(
        [
            sys.executable,
            "-c",
            "import sqlite3,sys;c=sqlite3.connect(sys.argv[1]);c.execute('PRAGMA journal_mode=WAL');"
            "c.execute('SELECT COUNT(*) FROM checkpoints').fetchone();print('held',flush=True);sys.stdin.read()",
            str(db),
        ],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        assert holder.stdout.readline().strip() == "held"
        res = migrate_legacy_auto_vacuum(db)
        assert res["status"] == "busy", res
        assert _pragma(db, "auto_vacuum") == 0
    finally:
        holder.communicate("", timeout=10)
    assert migrate_legacy_auto_vacuum(db)["status"] == "migrated"


@pytest.mark.platform_sensitive
def test_db_open_in_this_process_is_skipped(tmp_path):
    db = _legacy_db(tmp_path / "checkpoints.db")
    other = sqlite3.connect(db)
    other.execute("SELECT COUNT(*) FROM checkpoints").fetchone()
    try:
        assert migrate_legacy_auto_vacuum(db)["status"] == "busy"
    finally:
        other.close()


@pytest.mark.parametrize(
    "boom",
    ["connect", "vacuum", "disk_usage"],
)
def test_no_exception_escapes(tmp_path, monkeypatch, boom):
    db = _legacy_db(tmp_path / "checkpoints.db")
    rows = _rows(db)
    if boom == "connect":

        def _connect(*a, **k):
            raise sqlite3.OperationalError("unable to open database file")

        monkeypatch.setattr(checkpoint_migrate.sqlite3, "connect", _connect)
    elif boom == "disk_usage":

        def _du(p):
            raise OSError("no such volume")

        monkeypatch.setattr(checkpoint_migrate.shutil, "disk_usage", _du)
    else:
        real_connect = sqlite3.connect

        class _Conn:
            def __init__(self, conn):
                self._c = conn

            def execute(self, sql, *a):
                if sql.strip().upper() == "VACUUM":
                    raise sqlite3.OperationalError("disk I/O error")
                return self._c.execute(sql, *a)

            def __getattr__(self, name):
                return getattr(self._c, name)

        monkeypatch.setattr(checkpoint_migrate.sqlite3, "connect", lambda *a, **k: _Conn(real_connect(*a, **k)))

    res = migrate_legacy_auto_vacuum(db)
    assert res["status"] == "error", res
    monkeypatch.undo()
    assert _rows(db) == rows


def test_corrupt_file_does_not_raise(tmp_path):
    db = tmp_path / "checkpoints.db"
    db.write_bytes(b"this is not a sqlite database" * 100)
    assert migrate_legacy_auto_vacuum(db)["status"] == "error"


def test_cli_migrates_a_workspace_db(tmp_path, monkeypatch, capsys):
    from infra import paths as infra_paths

    class _Paths:
        workspaces_dir = tmp_path / "workspaces"

        @staticmethod
        def store(name):
            return tmp_path / name

    monkeypatch.setattr(infra_paths, "instance_paths", lambda: _Paths)
    member = tmp_path / "workspaces" / "brandLaunch"
    member.mkdir(parents=True)
    db = _legacy_db(member / "checkpoints.db")

    assert run_checkpoint_cli(["migrate", "--workspace", "brandLaunch"]) == 0
    assert "migrated" in capsys.readouterr().out
    assert _pragma(db, "auto_vacuum") == 2
    assert run_checkpoint_cli(["migrate", "--workspace", "brandLaunch"]) == 0
    assert "already" in capsys.readouterr().out
    # This instance's own DB doesn't exist here.
    assert run_checkpoint_cli(["migrate"]) == 2


def test_cli_is_registered_on_the_front_door():
    from server.cli import _FORWARD

    assert _FORWARD["checkpoint"] == ("graph.checkpoint_migrate", "run_checkpoint_cli")
