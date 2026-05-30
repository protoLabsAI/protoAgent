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
"""

from __future__ import annotations

import sqlite3
import uuid

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


def prune_checkpoints(
    db_path: str,
    *,
    keep_per_thread: int = 5,
    max_age_seconds: float | None = None,
    now: float | None = None,
) -> dict[str, int]:
    """Trim the checkpoint DB. Returns counts of what was removed.

    ``max_age_seconds=None`` disables the age TTL (only the per-thread cap runs).
    ``now`` is injectable for tests.
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
                rows = conn.execute(
                    "SELECT checkpoint_id FROM checkpoints WHERE thread_id=?", (thread_id,)
                ).fetchall()
                stamps = [t for t in (uuidv6_unix_seconds(r[0]) for r in rows) if t is not None]
                # Only TTL threads we can date *and* that are entirely old.
                if stamps and max(stamps) < cutoff:
                    conn.execute("DELETE FROM checkpoints WHERE thread_id=?", (thread_id,))
                    conn.execute("DELETE FROM writes WHERE thread_id=?", (thread_id,))
                    threads.remove(thread_id)
                    threads_deleted += 1

        # 2. Per-thread cap — keep the latest N checkpoints per namespace.
        keep = max(1, keep_per_thread)
        for thread_id in threads:
            for (ns,) in conn.execute(
                "SELECT DISTINCT checkpoint_ns FROM checkpoints WHERE thread_id=?", (thread_id,)
            ).fetchall():
                stale = [
                    r[0] for r in conn.execute(
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
