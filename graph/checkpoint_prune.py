"""Periodic pruning for the SQLite conversation checkpointer.

LangGraph writes ~3 checkpoint rows per turn (one per super-step), all retained
per ``thread_id`` — so the DB grows unbounded as chats accumulate. We don't use
time-travel/replay, only resume-from-latest, so older checkpoints are dead
weight. This trims the DB two ways:

- **Per-thread cap** — keep only the latest ``keep_per_thread`` checkpoints per
  ``(thread_id, checkpoint_ns)`` (resume needs only the most recent). Ordered by
  ``checkpoint_id``, which LangGraph generates as a time-sortable UUIDv6.
- **Age TTL** — delete whole threads whose newest checkpoint is older than
  ``max_age_days`` (idle conversations). The age comes from the UUIDv6
  timestamp, so no extra bookkeeping table is needed.

All pure SQL on a short-lived connection (the saver runs WAL mode, so this
coexists with live writes); failures are caught by the caller and never block.

After row deletions, ``reclaim()`` truncates the WAL and incrementally frees
pages back to the OS so the on-disk file shrinks rather than holding freed space
forever (a legacy ``auto_vacuum=NONE`` DB gets a full VACUUM only on opt-in).
"""

from __future__ import annotations

import logging
import sqlite3
import uuid

_log = logging.getLogger("protoagent.checkpoint_prune")

# Thread-id prefix of background (A2A) runs, which get ``background_keep``.
_BACKGROUND_PREFIX = "a2a:background:"

# 100ns intervals between the UUID (Gregorian, 1582-10-15) and Unix epochs.
_GREGORIAN_OFFSET = 0x01B21DD213814000


def uuidv6_unix_seconds(checkpoint_id: str) -> float | None:
    """Unix seconds encoded in a UUIDv6, or None if it isn't a parseable v6."""
    try:
        u = uuid.UUID(checkpoint_id)
    except (ValueError, AttributeError):
        return None
    if u.version != 6:
        return None
    i = u.int
    time_high = (i >> 96) & 0xFFFFFFFF
    time_mid = (i >> 80) & 0xFFFF
    time_low = (i >> 64) & 0x0FFF
    ticks = (time_high << 28) | (time_mid << 12) | time_low  # 100ns since 1582
    return (ticks - _GREGORIAN_OFFSET) / 1e7


def find_aged_threads(db_path: str, max_age_seconds: float, *, now: float | None = None) -> list[str]:
    """Thread ids whose newest checkpoint is older than the cutoff (datable via
    UUIDv6). Used to harvest a thread to knowledge *before* deleting it."""
    import time as _time

    cutoff = (now if now is not None else _time.time()) - max_age_seconds
    conn = sqlite3.connect(db_path, timeout=10)
    try:
        newest = _newest_stamps(conn)
    finally:
        conn.close()
    return [tid for tid, ts in newest.items() if ts is not None and ts < cutoff]


def _newest_stamps(conn: sqlite3.Connection) -> dict[str, float | None]:
    """Every thread id → its newest datable (UUIDv6) checkpoint time, ``None`` when none
    of its ids is datable. ONE scan of the table (#3973 — was a query per thread). The
    max is taken over parsed stamps, not ``MAX(checkpoint_id)``, so a non-v6 id that
    sorts high can't hide a thread's real age."""
    newest: dict[str, float | None] = {}
    for thread_id, checkpoint_id in conn.execute("SELECT thread_id, checkpoint_id FROM checkpoints"):
        ts = uuidv6_unix_seconds(checkpoint_id)
        prev = newest.get(thread_id)
        newest[thread_id] = ts if prev is None else (prev if ts is None else max(prev, ts))
    return newest


def thread_has_checkpoints_before(db_path: str, thread_id: str, before: float | None) -> bool:
    """Does ``thread_id`` hold a checkpoint written before ``before`` (unix seconds;
    ``None`` = any)? A checkpoint whose id isn't a datable UUIDv6 counts as before —
    the conservative answer for the forget sweep, whose "yes" restores rows."""
    conn = sqlite3.connect(db_path, timeout=10)
    try:
        rows = conn.execute("SELECT checkpoint_id FROM checkpoints WHERE thread_id=?", (thread_id,)).fetchall()
    finally:
        conn.close()
    for (cid,) in rows:
        if before is None:
            return True
        ts = uuidv6_unix_seconds(cid)
        if ts is None or ts < before:
            return True
    return False


def _like_escape(text: str) -> str:
    """``text`` as a literal LIKE prefix under ``ESCAPE '\\'``."""
    return text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def delete_thread(db_path: str, thread_id: str, *, cascade: bool = False) -> int:
    """Delete all checkpoints + writes for a thread. Returns checkpoints removed.

    When ``cascade`` is True, also deletes any sub-threads whose id starts with
    ``thread_id`` followed by ``:goal-iter-`` (goal-mode iteration checkpoints)."""
    conn = sqlite3.connect(db_path, timeout=10)
    conn.execute("PRAGMA busy_timeout=5000")
    try:
        if cascade:
            # ``%`` / ``_`` in the id are literals, not wildcards (#3973): an unescaped
            # ``a_b`` would also cascade into ``aXb:goal-iter-*``.
            where = "thread_id=? OR thread_id LIKE ? ESCAPE '\\'"
            args = (thread_id, _like_escape(thread_id) + ":goal-iter-%")
            n = conn.execute(f"SELECT COUNT(*) FROM checkpoints WHERE {where}", args).fetchone()[0]
            conn.execute(f"DELETE FROM checkpoints WHERE {where}", args)
            conn.execute(f"DELETE FROM writes WHERE {where}", args)
        else:
            n = conn.execute("SELECT COUNT(*) FROM checkpoints WHERE thread_id=?", (thread_id,)).fetchone()[0]
            conn.execute("DELETE FROM checkpoints WHERE thread_id=?", (thread_id,))
            conn.execute("DELETE FROM writes WHERE thread_id=?", (thread_id,))
        conn.commit()
        return n
    finally:
        conn.close()


def prune_checkpoints(
    db_path: str,
    *,
    keep_per_thread: int = 2,
    max_age_seconds: float | None = None,
    now: float | None = None,
    background_keep: int | None = None,
) -> dict[str, int]:
    """Trim the checkpoint DB. Returns counts of what was removed.

    ``max_age_seconds=None`` disables the age TTL (only the per-thread cap runs).
    ``now`` is injectable for tests.
    ``background_keep`` overrides the per-thread cap for ``a2a:background:*`` threads
    (resume-from-latest only — no time-travel, so retaining extras is waste).
    """
    conn = sqlite3.connect(db_path, timeout=10)
    conn.execute("PRAGMA busy_timeout=5000")
    threads_deleted = 0
    checkpoints_deleted = 0
    try:
        # 1. Age TTL — drop whole threads idle past the cutoff. One scan dates every
        #    thread (#3973 — was a query per thread).
        if max_age_seconds is not None:
            import time as _time

            cutoff = (now if now is not None else _time.time()) - max_age_seconds
            # Only TTL threads we can date *and* that are entirely old.
            aged = [(tid,) for tid, ts in _newest_stamps(conn).items() if ts is not None and ts < cutoff]
            conn.executemany("DELETE FROM checkpoints WHERE thread_id=?", aged)
            conn.executemany("DELETE FROM writes WHERE thread_id=?", aged)
            threads_deleted = len(aged)

        # 2. Per-thread cap — keep the latest N checkpoints per namespace, found in ONE
        #    windowed query (#3973 — was a query per thread and per namespace).
        #    Background threads get a tighter cap (resume-from-latest only).
        keep = max(1, keep_per_thread)
        bg_keep = keep if background_keep is None else max(1, background_keep)
        stale = conn.execute(
            "SELECT thread_id, checkpoint_ns, checkpoint_id FROM ("
            "  SELECT thread_id, checkpoint_ns, checkpoint_id, ROW_NUMBER() OVER ("
            "    PARTITION BY thread_id, checkpoint_ns ORDER BY checkpoint_id DESC) AS rn"
            "  FROM checkpoints)"
            " WHERE rn > CASE WHEN substr(thread_id, 1, ?) = ? THEN ? ELSE ? END",
            (len(_BACKGROUND_PREFIX), _BACKGROUND_PREFIX, bg_keep, keep),
        ).fetchall()
        conn.executemany("DELETE FROM checkpoints WHERE thread_id=? AND checkpoint_ns=? AND checkpoint_id=?", stale)
        conn.executemany("DELETE FROM writes WHERE thread_id=? AND checkpoint_ns=? AND checkpoint_id=?", stale)
        checkpoints_deleted = len(stale)

        conn.commit()
    finally:
        conn.close()
    return {"threads_deleted": threads_deleted, "checkpoints_deleted": checkpoints_deleted}


def reclaim(db_path: str, *, full_vacuum: bool = False) -> dict[str, int]:
    """Truncate the WAL and free unused DB pages back to the OS.

    Designed as a best-effort companion to ``prune_checkpoints``: after rows
    are deleted the DB file still holds their disk space (and the WAL may
    contain stale frames).  This call compacts both.

    * ``PRAGMA wal_checkpoint(TRUNCATE)`` — checkpoints the WAL and truncates
      it to zero, so the ``-wal`` file disappears.
    * ``PRAGMA incremental_vacuum`` — when ``auto_vacuum=INCREMENTAL`` (every DB
      ``build_sqlite_checkpointer`` created), frees pages from the freelist back
      to the OS (``page_count`` drops). Cheap; safe alongside live writes.
    * A legacy DB (``auto_vacuum=NONE``, created before the checkpointer set
      INCREMENTAL) is NOT vacuumed unless ``full_vacuum=True`` (#3973). A full
      ``VACUUM`` rewrites the whole file under an exclusive lock, stalling every
      live checkpoint write for its duration — too costly for a periodic sweep on
      a running server. Left alone, the freed pages stay on the freelist and are
      reused by later writes, so the file stops growing; it just doesn't shrink.
      ``full_vacuum=True`` is for an offline / maintenance caller (or run
      ``VACUUM`` with the server stopped), which also leaves the file at whatever
      ``auto_vacuum`` mode was last set.

    Returns ``{"wal_truncated": int, "pages_reclaimed": int}``.
    Best-effort: any error is caught and logged; the returned counts are zero
    on failure.  Never raises.
    """
    result: dict[str, int] = {"wal_truncated": 0, "pages_reclaimed": 0}
    try:
        conn = sqlite3.connect(db_path, timeout=10)
    except Exception:
        _log.exception("[checkpoint-prune] reclaim failed on connect")
        return result
    try:
        # 1. Checkpoint + truncate the WAL. TRUNCATE shrinks the -wal file to
        #    zero bytes (it does NOT delete the file). The result row is
        #    (busy, log_frames, checkpointed_frames); busy == 0 means the
        #    checkpoint completed — i.e. no other open connection held it back.
        row = conn.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
        result["wal_truncated"] = 1 if (row is not None and row[0] == 0) else 0

        # 2. Determine auto_vacuum mode.  INCREMENTAL (2) → use the cheap
        #    incremental_vacuum PRAGMA; anything else → a full VACUUM, only when
        #    the caller opted in (it holds an exclusive lock for the whole rewrite).
        av_row = conn.execute("PRAGMA auto_vacuum").fetchone()
        av_mode = av_row[0] if av_row else 0
        page_count_before = conn.execute("PRAGMA page_count").fetchone()[0]

        if av_mode == 2:  # INCREMENTAL
            conn.execute("PRAGMA incremental_vacuum")
        elif full_vacuum:
            conn.execute("VACUUM")
        else:
            _log.info(
                "[checkpoint-prune] %s predates auto_vacuum=INCREMENTAL — skipping the full "
                "VACUUM (it would lock out live writes); freed pages are reused. Run VACUUM "
                "with the server stopped to shrink the file.",
                db_path,
            )

        page_count_after = conn.execute("PRAGMA page_count").fetchone()[0]
        result["pages_reclaimed"] = max(0, page_count_before - page_count_after)
    except Exception:
        _log.exception("[checkpoint-prune] reclaim failed")
    finally:
        conn.close()
    return result
