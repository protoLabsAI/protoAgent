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


_BACKGROUND_PREFIX = "a2a:background:"


class ThreadedSqliteSaver(SqliteSaver):
    """A sync ``SqliteSaver`` whose async methods run on a worker thread, so the
    async agent graph can use it while history persists to a SQLite file.

    Two bounds on growth (ADR 0120), both applied on the write path so no turn can
    outrun them (the periodic prune sweep only runs every few hours):

    * **Retention on write** — after each ``put`` the ``(thread_id, checkpoint_ns)``
      keeps its newest ``keep_per_thread`` *root* checkpoints (``background_keep``
      for ``a2a:background:*``) plus anything newer than the oldest of them (an
      in-flight subagent's rows). Older rows and their writes are deleted.
    * **Media offload** — done by its serializer (``graph.checkpoint_serde``): large
      base64 / data-URI payloads in message content blocks are stored once per DB in
      ``checkpoint_media`` and restored on read, so a stock ``SqliteSaver`` with the
      same serde reads byte-identical messages.
    """

    keep_per_thread: int = 2
    background_keep: int = 1
    prune_on_write: bool = True

    @property
    def offload_media(self) -> bool:
        """Media offload lives in the serializer (``graph.checkpoint_serde``); this is
        its switch. Reads restore offloaded media either way."""
        return bool(getattr(self.serde, "offload", False))

    @offload_media.setter
    def offload_media(self, value: bool) -> None:
        if hasattr(self.serde, "offload"):
            self.serde.offload = bool(value)

    def put(self, config, checkpoint, metadata, new_versions):
        out = super().put(config, checkpoint, metadata, new_versions)
        if self.prune_on_write:
            thread_id = str(config["configurable"]["thread_id"])
            try:
                self._trim(
                    thread_id, str(config["configurable"].get("checkpoint_ns", "")), written_id=checkpoint["id"]
                )
            except sqlite3.Error:
                log.warning("[checkpointer] retention-on-write trim failed for %s", thread_id, exc_info=True)
        return out

    def _trim(self, thread_id: str, checkpoint_ns: str, written_id: str | None = None) -> int:
        """Retention on write: ``graph.checkpoint_prune.trim_group`` on this saver's
        connection, under its lock."""
        from graph.checkpoint_prune import trim_group

        keep = self.background_keep if thread_id.startswith(_BACKGROUND_PREFIX) else self.keep_per_thread
        with self.cursor() as cur:
            return trim_group(cur.connection, thread_id, checkpoint_ns, keep, written_id=written_id)

    # ── async delegation ──────────────────────────────────────────────────────
    async def aget_tuple(self, config):
        return await asyncio.to_thread(self.get_tuple, config)

    async def aput(self, *args, **kwargs):
        return await asyncio.to_thread(_retry_on_locked, self.put, *args, **kwargs)

    async def aput_writes(self, *args, **kwargs):
        return await asyncio.to_thread(_retry_on_locked, self.put_writes, *args, **kwargs)

    async def adelete_thread(self, thread_id: str) -> None:
        return await asyncio.to_thread(self.delete_thread, thread_id)

    async def alist(self, *args, **kwargs):
        # The base `list` is a sync generator; materialize it off-thread, then
        # re-yield (alist must itself be an async generator).
        for item in await asyncio.to_thread(lambda: [*self.list(*args, **kwargs)]):
            yield item


def build_sqlite_checkpointer(db_path: str) -> ThreadedSqliteSaver:
    """Open (or create) the checkpoint DB and return a ready saver."""
    conn = sqlite3.connect(db_path, check_same_thread=False)
    # WAL lets the periodic pruner (separate connection) run while the agent
    # writes; busy_timeout avoids spurious "database is locked" under contention.
    # busy_timeout FIRST: the WAL pragma itself takes a lock (#2428).
    conn.execute("PRAGMA busy_timeout=5000")
    # auto_vacuum=INCREMENTAL must be set while the file is still EMPTY, so freed
    # pages are tracked in the freelist and the pruner can hand them back with
    # ``incremental_vacuum`` instead of a full VACUUM rewrite. That means before
    # ``journal_mode=WAL`` too: switching to WAL writes the DB header, after which
    # the auto_vacuum pragma is a silent no-op. It used to run second, so every DB
    # this function created was auto_vacuum=NONE and never shrank (34 GB files
    # holding 85 MB of rows). On an existing DB this pragma changes nothing; a
    # NONE DB is migrated at boot by graph/checkpoint_migrate.py.
    conn.execute("PRAGMA auto_vacuum=INCREMENTAL")
    conn.execute("PRAGMA journal_mode=WAL")
    from graph.checkpoint_serde import protoagent_serde

    saver = ThreadedSqliteSaver(conn, serde=protoagent_serde(conn))
    saver.setup()  # create the checkpoint tables if absent (idempotent)
    return saver


def configure_saver(saver, config) -> None:
    """Apply the live ``checkpoint.*`` retention knobs to a saver (no-op for any other
    saver type). Called at boot and on every maintenance pass, so a settings change
    takes effect without a restart."""
    if not isinstance(saver, ThreadedSqliteSaver) or config is None:
        return
    saver.keep_per_thread = max(1, int(getattr(config, "checkpoint_keep_per_thread", 2) or 1))
    saver.background_keep = max(1, int(getattr(config, "checkpoint_background_keep", 1) or 1))
    saver.prune_on_write = bool(getattr(config, "checkpoint_prune_on_write", True))
    saver.offload_media = bool(getattr(config, "checkpoint_offload_media", True))
