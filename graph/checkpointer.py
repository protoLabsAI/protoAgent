"""Durable conversation checkpointer (SQLite) for the agent graph.

LangGraph needs the checkpointer bound at **compile time** so multi-turn chats
keep their history per ``thread_id``. Two constraints shape the choice here:

- The graph is compiled **synchronously at boot**, before uvicorn starts the
  event loop — so an aiosqlite-based ``AsyncSqliteSaver`` (which wants a running
  loop at construction/setup) is an awkward fit.
- The agent runs **async** (``astream_events``), and the stock sync
  ``SqliteSaver`` raises ``NotImplementedError`` on the async methods.

So we wrap the sync ``SqliteSaver`` and delegate its async methods to worker
threads via ``asyncio.to_thread``: synchronous construction (no loop needed),
loop-agnostic at call time, durable on disk. The saver serializes access with
its own lock and ``check_same_thread=False``, which keeps the cross-thread
``to_thread`` calls safe.
"""

from __future__ import annotations

import asyncio
import logging
import sqlite3
import time

from langgraph.checkpoint.sqlite import SqliteSaver

log = logging.getLogger(__name__)

# WAL + a 5s busy_timeout absorb almost all checkpoint-write contention, but a
# writer that holds the lock *past* the busy_timeout (e.g. the incremental pruner
# mid-VACUUM, or two background turns landing at once) still surfaces a
# ``database is locked`` OperationalError. That killed a whole background turn
# mid-checkpoint (#1738) — the worst class of failure for an autonomous agent, a
# silent loss of committed work. A locked write commits nothing, so a bounded
# retry with short exponential backoff is safe and recovers the turn.
_LOCK_WRITE_RETRIES = 3
_LOCK_BACKOFF_S = 0.1


def _retry_on_locked(fn, *args, **kwargs):
    """Run a sync checkpointer write, retrying ``database is locked`` a few times
    before giving up. Only that specific OperationalError is retried; every other
    error (and the final locked attempt) propagates unchanged."""
    delay = _LOCK_BACKOFF_S
    for attempt in range(_LOCK_WRITE_RETRIES):
        try:
            return fn(*args, **kwargs)
        except sqlite3.OperationalError as exc:
            last = attempt == _LOCK_WRITE_RETRIES - 1
            if "database is locked" not in str(exc).lower() or last:
                raise
            log.warning(
                "checkpoint %s: database is locked, retry %d/%d in %.2fs",
                getattr(fn, "__name__", fn),
                attempt + 1,
                _LOCK_WRITE_RETRIES - 1,
                delay,
            )
            time.sleep(delay)
            delay *= 2


class ThreadedSqliteSaver(SqliteSaver):
    """A sync ``SqliteSaver`` whose async methods run on a worker thread, so the
    async agent graph can use it while history persists to a SQLite file."""

    async def aget_tuple(self, config):
        return await asyncio.to_thread(self.get_tuple, config)

    async def aput(self, *args, **kwargs):
        return await asyncio.to_thread(_retry_on_locked, self.put, *args, **kwargs)

    async def aput_writes(self, *args, **kwargs):
        return await asyncio.to_thread(_retry_on_locked, self.put_writes, *args, **kwargs)

    async def alist(self, *args, **kwargs):
        # The base `list` is a sync generator; materialize it off-thread, then
        # re-yield (alist must itself be an async generator).
        for item in await asyncio.to_thread(lambda: list(self.list(*args, **kwargs))):
            yield item


def build_sqlite_checkpointer(db_path: str) -> ThreadedSqliteSaver:
    """Open (or create) the checkpoint DB and return a ready saver."""
    conn = sqlite3.connect(db_path, check_same_thread=False)
    # WAL lets the periodic pruner (separate connection) run while the agent
    # writes; busy_timeout avoids spurious "database is locked" under contention.
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    # auto_vacuum=INCREMENTAL must be set BEFORE any tables are created so
    # freed pages are tracked in the freelist — the pruner can then reclaim
    # them on demand without a full VACUUM rewrite.
    conn.execute("PRAGMA auto_vacuum=INCREMENTAL")
    saver = ThreadedSqliteSaver(conn)
    saver.setup()  # create the checkpoint tables if absent (idempotent)
    return saver
