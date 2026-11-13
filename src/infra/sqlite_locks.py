"""Per-file write locks for the process's SQLite stores.

SQLite's busy handler only polls (sleep, retry), so a connection waiting on
BEGIN IMMEDIATE can sleep through every gap between another connection's
back-to-back transactions and raise "database is locked" after busy_timeout,
even though no single transaction held the lock that long. A blocking
``threading.Lock`` instead wakes the waiter when the holder releases.
busy_timeout still covers other processes.

:func:`file_lock` returns one lock per database file, shared by every store on
that file in this process. Keyed by the normcased real path so two spellings of
one file (a symlink, a relative path, a case-variant on Windows) share a lock.
"""

from __future__ import annotations

import os
import threading
from pathlib import Path

_FILE_LOCKS: dict[str, threading.Lock] = {}
_FILE_LOCKS_GUARD = threading.Lock()


def file_lock(path: str | Path) -> threading.Lock:
    """The lock guarding writes to the SQLite file at ``path``, shared across every
    store that opens the same file in this process."""
    key = os.path.normcase(os.path.realpath(path))
    with _FILE_LOCKS_GUARD:
        return _FILE_LOCKS.setdefault(key, threading.Lock())
