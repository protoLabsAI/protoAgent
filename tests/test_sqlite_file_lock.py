"""The shared per-file SQLite write lock (src/infra/sqlite_locks.py).

One lock per database file in this process, keyed by the normcased real path so
that two spellings of one file share it and two different files don't. The tasks
store (and, in slice 2, the scheduler/background/metrics stores) take this lock
around each write transaction so a waiter queues on a blocking lock instead of
losing SQLite's busy-handler poll race. See :func:`infra.sqlite_locks.file_lock`.
"""

from __future__ import annotations

import sqlite3
import threading
import time
from pathlib import Path

import pytest

from background.store import BackgroundStore
from infra.sqlite_locks import file_lock
from observability.metrics_store import MetricsStore
from scheduler.local import LocalScheduler


@pytest.mark.parametrize("spelling", [Path, str], ids=["path", "str"])
def test_same_path_returns_the_same_lock(tmp_path, spelling):
    """Looking one file up twice returns the same lock — and by either a ``Path`` or
    ``str`` spelling, because the key is the normcased real path, not the argument type."""
    db = tmp_path / "issues.db"
    lock = file_lock(spelling(db))
    assert lock is file_lock(db)
    assert lock is file_lock(str(db))
    assert isinstance(lock, type(threading.Lock()))


@pytest.mark.platform_sensitive
def test_two_paths_to_one_file_share_a_lock(tmp_path):
    """A symlink (or any alternate spelling) resolves to one real path, so the two
    get the same lock — the whole point of keying on the real path."""
    real = tmp_path / "issues.db"
    real.write_bytes(b"")  # realpath only collapses the link once the target exists
    link = tmp_path / "issues-link.db"
    try:
        link.symlink_to(real)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks unavailable")
    assert file_lock(link) is file_lock(real)


def test_different_files_get_different_locks(tmp_path):
    assert file_lock(tmp_path / "a.db") is not file_lock(tmp_path / "b.db")


# ── the per-file lock defeats SQLite's busy-handler poll race (#5122) ──────────
# Slice 2: the scheduler, background and metrics stores take file_lock(path) around
# their write transactions. These deterministic tests prove a second instance waiting
# on a write queues on that blocking lock and SUCCEEDS, instead of losing SQLite's
# busy-handler poll and raising "database is locked" after busy_timeout.
#
# The holder stands in for the other instance's in-flight write: it takes the SHARED
# lock AND an open ``BEGIN IMMEDIATE`` (the SQLite write lock) and holds both for 300 ms.
# The waiter's connections use a 1 ms busy_timeout, so had its write NOT taken the
# shared lock it would lose the poll within ~1 ms — the control below proves that race
# is real, and the waiter proves the lock defeats it.

_HOLD_S = 0.3


def _use_1ms_busy_timeout(store) -> None:
    """Point a store's per-call connections at a 1 ms busy_timeout, so that a write
    which did NOT take the shared lock raises 'database is locked' almost immediately
    under contention."""
    original = store._connect

    def connect() -> sqlite3.Connection:
        db = original()
        db.execute("PRAGMA busy_timeout=1")
        return db

    store._connect = connect


def _assert_waiter_blocks_then_succeeds(path, lock, waiter_write, verify) -> None:
    started = threading.Event()

    def hold_write_open() -> None:
        with lock:
            conn = sqlite3.connect(str(path))
            try:
                conn.execute("PRAGMA busy_timeout=5000")
                conn.execute("PRAGMA journal_mode=WAL")
                conn.execute("BEGIN IMMEDIATE")  # take and keep the SQLite write lock
                started.set()
                time.sleep(_HOLD_S)
                conn.commit()
            finally:
                conn.close()

    holder = threading.Thread(target=hold_write_open, name="file-lock-holder")
    holder.start()
    try:
        assert started.wait(5), "holder never took the write lock"

        # Control: a connection that does NOT take the shared lock loses the
        # busy-handler poll within its 1 ms budget — the exact bug the lock fixes.
        control = sqlite3.connect(str(path))
        control.execute("PRAGMA busy_timeout=1")
        with pytest.raises(sqlite3.OperationalError, match="database is locked"):
            control.execute("BEGIN IMMEDIATE")
        control.close()

        # The real store write (1 ms busy_timeout) must block on the shared lock until
        # the holder commits ~300 ms later, then succeed — never raise.
        begin = time.monotonic()
        waiter_write()
        waited = time.monotonic() - begin
    finally:
        holder.join(5)

    assert waited >= 0.1, f"the write returned in {waited:.3f}s — it did not wait on the lock"
    assert verify(), "the waiter's write did not land"


def test_metrics_store_waiter_waits_for_the_file_lock(tmp_path):
    db = tmp_path / "metrics.db"
    holder = MetricsStore(str(db))
    waiter = MetricsStore(str(db))
    assert holder._lock is waiter._lock  # two instances, one file → one lock
    _use_1ms_busy_timeout(waiter)
    _assert_waiter_blocks_then_succeeds(
        waiter.path,
        waiter._lock,
        lambda: waiter.record("series", 1.0),
        lambda: waiter.last("series") is not None,
    )


def test_background_store_waiter_waits_for_the_file_lock(tmp_path):
    db = tmp_path / "background.db"
    holder = BackgroundStore(str(db))
    waiter = BackgroundStore(str(db))
    assert holder._lock is waiter._lock
    _use_1ms_busy_timeout(waiter)
    created: dict[str, str] = {}

    def write() -> None:
        created["id"] = waiter.create(
            agent_name="a", origin_session="sess", subagent_type="t", description="d", prompt="p"
        )

    _assert_waiter_blocks_then_succeeds(
        waiter.path,
        waiter._lock,
        write,
        lambda: waiter.get(created["id"]) is not None,
    )


def test_scheduler_store_waiter_waits_for_the_file_lock(tmp_path, monkeypatch):
    monkeypatch.delenv("SCHEDULER_DB_DIR", raising=False)
    holder = LocalScheduler("waiter-agent", invoke_url="http://localhost:0", db_dir=str(tmp_path))
    waiter = LocalScheduler("waiter-agent", invoke_url="http://localhost:0", db_dir=str(tmp_path))
    assert holder.path == waiter.path
    assert holder._lock is waiter._lock
    _use_1ms_busy_timeout(waiter)
    _assert_waiter_blocks_then_succeeds(
        waiter.path,
        waiter._lock,
        lambda: waiter.add_job("hi", "* * * * *"),
        lambda: len(waiter.list_jobs()) == 1,
    )
