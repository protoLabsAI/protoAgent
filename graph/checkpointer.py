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
import sqlite3

from langgraph.checkpoint.sqlite import SqliteSaver


class ThreadedSqliteSaver(SqliteSaver):
    """A sync ``SqliteSaver`` whose async methods run on a worker thread, so the
    async agent graph can use it while history persists to a SQLite file."""

    async def aget_tuple(self, config):
        return await asyncio.to_thread(self.get_tuple, config)

    async def aput(self, *args, **kwargs):
        return await asyncio.to_thread(self.put, *args, **kwargs)

    async def aput_writes(self, *args, **kwargs):
        return await asyncio.to_thread(self.put_writes, *args, **kwargs)

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
    saver = ThreadedSqliteSaver(conn)
    saver.setup()  # create the checkpoint tables if absent (idempotent)
    return saver
