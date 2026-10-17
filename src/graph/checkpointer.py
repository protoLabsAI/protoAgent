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
    * **Media offload** — large base64 / data-URI payloads in message content blocks
      are stored once per thread in ``checkpoint_blobs`` and referenced from the
      checkpoint, then restored on read (``graph.checkpoint_media``).
    """

    keep_per_thread: int = 2
    background_keep: int = 1
    prune_on_write: bool = True
    offload_media: bool = True

    def setup(self) -> None:
        first = not self.is_setup
        super().setup()
        if first:
            from graph.checkpoint_media import BLOB_TABLE_DDL

            self.conn.execute(BLOB_TABLE_DDL)
            self.conn.commit()

    # ── write path ────────────────────────────────────────────────────────────
    def _store_blobs(self, thread_id: str, blobs: dict[str, str]) -> None:
        if not blobs:
            return
        hashes = list(blobs)
        with self.cursor() as cur:
            have: set[str] = set()
            for k in range(0, len(hashes), 500):
                chunk = hashes[k : k + 500]
                q = f"SELECT hash FROM checkpoint_blobs WHERE thread_id=? AND hash IN ({','.join('?' * len(chunk))})"
                have.update(r[0] for r in cur.execute(q, (thread_id, *chunk)))
            cur.executemany(
                "INSERT OR IGNORE INTO checkpoint_blobs (thread_id, hash, data, created) VALUES (?, ?, ?, ?)",
                [(thread_id, h, blobs[h].encode("utf-8", "surrogatepass"), time.time()) for h in hashes if h not in have],
            )

    def put(self, config, checkpoint, metadata, new_versions):
        thread_id = str(config["configurable"]["thread_id"])
        if self.offload_media:
            from graph.checkpoint_media import offload_channel_values

            blobs: dict[str, str] = {}
            cv = checkpoint.get("channel_values") or {}
            try:
                new_cv = offload_channel_values(cv, blobs)
            except Exception:  # noqa: BLE001 — fail OPEN: store inline, never brick the thread
                log.warning("[checkpointer] media offload failed for %s; storing inline", thread_id, exc_info=True)
                new_cv, blobs = cv, {}
            if new_cv is not cv:
                checkpoint = {**checkpoint, "channel_values": new_cv}
            # Payloads land BEFORE the row that references them: a crash in between
            # leaves an unreferenced blob (GC'd later), never a dangling reference.
            self._store_blobs(thread_id, blobs)
        out = super().put(config, checkpoint, metadata, new_versions)
        if self.prune_on_write:
            try:
                self._trim(
                    thread_id, str(config["configurable"].get("checkpoint_ns", "")), written_id=checkpoint["id"]
                )
            except sqlite3.Error:
                log.warning("[checkpointer] retention-on-write trim failed for %s", thread_id, exc_info=True)
        return out

    def put_writes(self, config, writes, task_id, task_path=""):
        if self.offload_media:
            from graph.checkpoint_media import offload_value

            blobs: dict[str, str] = {}
            try:
                mapped = [(ch, offload_value(v, blobs)) for ch, v in writes]
            except Exception:  # noqa: BLE001 — fail OPEN, as in put()
                log.warning("[checkpointer] media offload failed for pending writes; storing inline", exc_info=True)
                blobs = {}
            if blobs:
                self._store_blobs(str(config["configurable"]["thread_id"]), blobs)
                writes = mapped
        return super().put_writes(config, writes, task_id, task_path)

    def _trim(self, thread_id: str, checkpoint_ns: str, written_id: str | None = None) -> int:
        """Retention on write: ``graph.checkpoint_prune.trim_group`` on this saver's
        connection, under its lock."""
        from graph.checkpoint_prune import trim_group

        keep = self.background_keep if thread_id.startswith(_BACKGROUND_PREFIX) else self.keep_per_thread
        with self.cursor() as cur:
            return trim_group(cur.connection, thread_id, checkpoint_ns, keep, written_id=written_id)

    # ── read path ─────────────────────────────────────────────────────────────
    def _rehydrate(self, tup):
        if tup is None:
            return None
        from graph.checkpoint_media import refs_in_value, rehydrate_value

        cv = (tup.checkpoint or {}).get("channel_values") or {}
        found: set[str] = set()
        for v in cv.values():
            refs_in_value(v, found)
        for _t, _ch, v in tup.pending_writes or ():
            refs_in_value(v, found)
        if not found:
            return tup
        thread_id = str(tup.config["configurable"]["thread_id"])
        hashes = sorted(found)
        payloads: dict[str, str] = {}
        with self.cursor(transaction=False) as cur:
            for k in range(0, len(hashes), 500):
                chunk = hashes[k : k + 500]
                q = f"SELECT hash, data FROM checkpoint_blobs WHERE thread_id=? AND hash IN ({','.join('?' * len(chunk))})"
                payloads.update((h, bytes(d).decode("utf-8", "surrogatepass")) for h, d in cur.execute(q, (thread_id, *chunk)))
        new_cv = {k: rehydrate_value(v, payloads) for k, v in cv.items()}
        writes = [(t, ch, rehydrate_value(v, payloads)) for t, ch, v in (tup.pending_writes or ())]
        return tup._replace(checkpoint={**tup.checkpoint, "channel_values": new_cv}, pending_writes=writes)

    def get_tuple(self, config):
        return self._rehydrate(super().get_tuple(config))

    def list(self, config, *, filter=None, before=None, limit=None):
        # Materialize first: the base generator holds the saver lock while it yields,
        # and rehydrating takes it again.
        rows = [*super().list(config, filter=filter, before=before, limit=limit)]
        for tup in rows:
            yield self._rehydrate(tup)

    def delete_thread(self, thread_id: str) -> None:
        super().delete_thread(thread_id)
        with self.cursor() as cur:
            cur.execute("DELETE FROM checkpoint_blobs WHERE thread_id = ?", (str(thread_id),))

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
    saver = ThreadedSqliteSaver(conn)
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
