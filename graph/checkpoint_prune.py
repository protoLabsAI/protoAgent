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

After row deletions, ``reclaim()`` truncates the WAL and vacuum-frees pages
back to the OS so the on-disk file shrinks rather than holding freed space
forever.
"""

from __future__ import annotations

import logging
import os
import sqlite3
import uuid

_log = logging.getLogger("protoagent.checkpoint_prune")

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
        aged: list[str] = []
        for (thread_id,) in conn.execute("SELECT DISTINCT thread_id FROM checkpoints"):
            rows = conn.execute("SELECT checkpoint_id FROM checkpoints WHERE thread_id=?", (thread_id,)).fetchall()
            stamps = [t for t in (uuidv6_unix_seconds(r[0]) for r in rows) if t is not None]
            if stamps and max(stamps) < cutoff:
                aged.append(thread_id)
        return aged
    finally:
        conn.close()


def delete_thread(db_path: str, thread_id: str, *, cascade: bool = False) -> int:
    """Delete all checkpoints + writes for a thread. Returns checkpoints removed.

    When ``cascade`` is True, also deletes any sub-threads whose id starts with
    ``thread_id`` followed by ``:goal-iter-`` (goal-mode iteration checkpoints)."""
    conn = sqlite3.connect(db_path, timeout=10)
    conn.execute("PRAGMA busy_timeout=5000")
    try:
        if cascade:
            n = conn.execute(
                "SELECT COUNT(*) FROM checkpoints WHERE thread_id=? OR thread_id LIKE ? || ':goal-iter-%'",
                (thread_id, thread_id),
            ).fetchone()[0]
            conn.execute(
                "DELETE FROM checkpoints WHERE thread_id=? OR thread_id LIKE ? || ':goal-iter-%'",
                (thread_id, thread_id),
            )
            conn.execute(
                "DELETE FROM writes WHERE thread_id=? OR thread_id LIKE ? || ':goal-iter-%'",
                (thread_id, thread_id),
            )
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
        threads = [r[0] for r in conn.execute("SELECT DISTINCT thread_id FROM checkpoints")]

        # 1. Age TTL — drop whole threads idle past the cutoff.
        if max_age_seconds is not None:
            import time as _time

            cutoff = (now if now is not None else _time.time()) - max_age_seconds
            for thread_id in list(threads):
                rows = conn.execute("SELECT checkpoint_id FROM checkpoints WHERE thread_id=?", (thread_id,)).fetchall()
                stamps = [t for t in (uuidv6_unix_seconds(r[0]) for r in rows) if t is not None]
                # Only TTL threads we can date *and* that are entirely old.
                if stamps and max(stamps) < cutoff:
                    conn.execute("DELETE FROM checkpoints WHERE thread_id=?", (thread_id,))
                    conn.execute("DELETE FROM writes WHERE thread_id=?", (thread_id,))
                    threads.remove(thread_id)
                    threads_deleted += 1

        # 2. Per-thread cap — keep the latest N checkpoints per namespace.
        #    Background threads get a tighter cap (resume-from-latest only).
        for thread_id in threads:
            if background_keep is not None and thread_id.startswith("a2a:background:"):
                keep = max(1, background_keep)
            else:
                keep = max(1, keep_per_thread)
            for (ns,) in conn.execute(
                "SELECT DISTINCT checkpoint_ns FROM checkpoints WHERE thread_id=?", (thread_id,)
            ).fetchall():
                stale = [
                    r[0]
                    for r in conn.execute(
                        "SELECT checkpoint_id FROM checkpoints WHERE thread_id=? AND checkpoint_ns=? "
                        "ORDER BY checkpoint_id DESC LIMIT -1 OFFSET ?",
                        (thread_id, ns, keep),
                    ).fetchall()
                ]
                for cid in stale:
                    conn.execute(
                        "DELETE FROM checkpoints WHERE thread_id=? AND checkpoint_ns=? AND checkpoint_id=?",
                        (thread_id, ns, cid),
                    )
                    conn.execute(
                        "DELETE FROM writes WHERE thread_id=? AND checkpoint_ns=? AND checkpoint_id=?",
                        (thread_id, ns, cid),
                    )
                    checkpoints_deleted += 1

        conn.commit()
    finally:
        conn.close()
    return {"threads_deleted": threads_deleted, "checkpoints_deleted": checkpoints_deleted}


def reclaim(db_path: str) -> dict[str, int]:
    """Truncate the WAL and free unused DB pages back to the OS.

    Designed as a best-effort companion to ``prune_checkpoints``: after rows
    are deleted the DB file still holds their disk space (and the WAL may
    contain stale frames).  This call compacts both.

    * ``PRAGMA wal_checkpoint(TRUNCATE)`` — checkpoints the WAL and truncates
      it to zero, so the ``-wal`` file disappears.
    * ``PRAGMA incremental_vacuum`` — when ``auto_vacuum=INCREMENTAL``, frees
      pages from the freelist back to the OS (``page_count`` drops). On a
      legacy DB with ``auto_vacuum=NONE``, a full ``VACUUM`` rewrites the
      entire file — slower but still shrinks it.

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
        # 1. Truncate the WAL so the -wal file shrinks / disappears.
        wal_path = db_path + "-wal"
        had_wal = os.path.exists(wal_path)
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        result["wal_truncated"] = 1 if (had_wal and not os.path.exists(wal_path)) else 0

        # 2. Determine auto_vacuum mode.  INCREMENTAL (2) → use the cheap
        #    incremental_vacuum PRAGMA; NONE (0) → fall back to full VACUUM.
        av_row = conn.execute("PRAGMA auto_vacuum").fetchone()
        av_mode = av_row[0] if av_row else 0
        page_count_before = conn.execute("PRAGMA page_count").fetchone()[0]

        if av_mode == 2:  # INCREMENTAL
            conn.execute("PRAGMA incremental_vacuum")
        else:
            conn.execute("VACUUM")

        page_count_after = conn.execute("PRAGMA page_count").fetchone()[0]
        result["pages_reclaimed"] = max(0, page_count_before - page_count_after)
    except Exception:
        _log.exception("[checkpoint-prune] reclaim failed")
    finally:
        conn.close()
    return result
