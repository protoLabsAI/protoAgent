"""The shared per-file SQLite write lock (src/infra/sqlite_locks.py).

One lock per database file in this process, keyed by the normcased real path so
that two spellings of one file share it and two different files don't. The tasks
store (and, in slice 2, the scheduler/background/metrics stores) take this lock
around each write transaction so a waiter queues on a blocking lock instead of
losing SQLite's busy-handler poll race. See :func:`infra.sqlite_locks.file_lock`.
"""

from __future__ import annotations

import threading

import pytest

from infra.sqlite_locks import file_lock


def test_same_path_returns_the_same_lock(tmp_path):
    db = tmp_path / "issues.db"
    lock = file_lock(db)
    assert lock is file_lock(db)
    assert isinstance(lock, type(threading.Lock()))


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


def test_str_and_path_spellings_share_a_lock(tmp_path):
    db = tmp_path / "issues.db"
    assert file_lock(str(db)) is file_lock(db)
