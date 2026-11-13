"""The shared per-file SQLite write lock (src/infra/sqlite_locks.py).

One lock per database file in this process, keyed by the normcased real path so
that two spellings of one file share it and two different files don't. The tasks
store (and, in slice 2, the scheduler/background/metrics stores) take this lock
around each write transaction so a waiter queues on a blocking lock instead of
losing SQLite's busy-handler poll race. See :func:`infra.sqlite_locks.file_lock`.
"""

from __future__ import annotations

import threading
from pathlib import Path

import pytest

from infra.sqlite_locks import file_lock


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
