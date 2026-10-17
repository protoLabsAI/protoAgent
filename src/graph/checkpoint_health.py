"""Checkpoint DB size alerts (ADR 0120).

A checkpoint DB can grow tens of GB in an afternoon (one visual-review turn on
brandLaunch: 87 GB in five hours, with nobody told until the disk was at 99%). These
helpers turn the DB's size into the live, self-clearing runtime-status warning the
console already banners (``GET /api/runtime/status`` ``warnings[]``):

* :func:`db_size_warning` — this instance: the DB file (+ WAL) over
  ``checkpoint.alert_db_mb``, or any single thread holding more than
  ``checkpoint.alert_thread_mb`` (rows + offloaded media), named with its size.
* :func:`fleet_size_warning` — the hub: each local fleet member's DB file over the
  same threshold, by ``stat`` only (the hub never opens a member's live DB).

Both are cheap: the per-thread breakdown is a ``length()`` aggregate (SQLite reads
the row header, not the blob) and is cached for a minute per DB.
"""

from __future__ import annotations

import os
import sqlite3
import time

_MB = 1024 * 1024
_TTL_S = 60.0
_cache: dict[str, tuple[float, dict]] = {}


def _human(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.0f} {unit}" if unit in ("B", "KB") else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"  # pragma: no cover


def file_bytes(db_path: str) -> int:
    """The DB file plus its WAL (what the disk actually holds)."""
    total = 0
    for p in (db_path, db_path + "-wal"):
        try:
            total += os.path.getsize(p)
        except OSError:
            pass
    return total


def db_stats(db_path: str, *, top: int = 3, now: float | None = None) -> dict:
    """``{"file_bytes", "threads": [(thread_id, bytes, checkpoints), ...]}`` — the
    biggest threads first, counting checkpoint rows, pending writes and offloaded
    media. Cached ``_TTL_S`` per path. Never raises (an unreadable DB → no threads)."""
    t = now if now is not None else time.monotonic()
    hit = _cache.get(db_path)
    if hit is not None and t - hit[0] < _TTL_S:
        return hit[1]
    stats: dict = {"file_bytes": file_bytes(db_path), "threads": []}
    try:
        conn = sqlite3.connect(db_path, timeout=2)
        try:
            sizes: dict[str, list[int]] = {}
            for tid, b, n in conn.execute(
                "SELECT thread_id, SUM(length(checkpoint)), COUNT(*) FROM checkpoints GROUP BY thread_id"
            ):
                sizes[tid] = [int(b or 0), int(n)]
            for tid, b in conn.execute("SELECT thread_id, SUM(length(value)) FROM writes GROUP BY thread_id"):
                sizes.setdefault(tid, [0, 0])[0] += int(b or 0)
            has_blobs = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='checkpoint_blobs'"
            ).fetchone()
            if has_blobs:
                for tid, b in conn.execute(
                    "SELECT thread_id, SUM(length(data)) FROM checkpoint_blobs GROUP BY thread_id"
                ):
                    sizes.setdefault(tid, [0, 0])[0] += int(b or 0)
        finally:
            conn.close()
        ranked = sorted(sizes.items(), key=lambda kv: kv[1][0], reverse=True)[:top]
        stats["threads"] = [(tid, b, n) for tid, (b, n) in ranked]
    except sqlite3.Error:
        pass
    _cache[db_path] = (t, stats)
    return stats


def db_size_warning(db_path: str | None, config) -> str | None:
    """This instance's checkpoint-size warning, or None. Thresholds (MB, 0 = off):
    ``checkpoint_alert_db_mb`` for the whole file, ``checkpoint_alert_thread_mb`` for
    any one thread."""
    if not db_path or config is None:
        return None
    db_mb = int(getattr(config, "checkpoint_alert_db_mb", 0) or 0)
    thread_mb = int(getattr(config, "checkpoint_alert_thread_mb", 0) or 0)
    if db_mb <= 0 and thread_mb <= 0:
        return None
    try:
        stats = db_stats(db_path)
    except Exception:  # noqa: BLE001 — a status-poll warning must never raise
        return None
    size = stats["file_bytes"]
    big = [(tid, b, n) for tid, b, n in stats["threads"] if thread_mb > 0 and b > thread_mb * _MB]
    if not (db_mb > 0 and size > db_mb * _MB) and not big:
        return None
    parts = [f"Conversation history (checkpoints.db) is {_human(size)}"]
    if db_mb > 0 and size > db_mb * _MB:
        parts[0] += f", over the {_human(db_mb * _MB)} alert threshold"
    top = big or stats["threads"][:1]
    if top:
        parts.append(
            "largest thread"
            + ("s" if len(top) > 1 else "")
            + ": "
            + ", ".join(f"{tid} ({_human(b)}, {n} checkpoint{'s' if n != 1 else ''})" for tid, b, n in top)
        )
    return (
        "; ".join(parts)
        + ". Check the disk, then reclaim with `protoagent checkpoint compact` (dry run by default; ADR 0120). "
        "Thresholds: History → checkpoint.alert_db_mb / alert_thread_mb."
    )


def fleet_size_warning(config) -> str | None:
    """The hub's warning for LOCAL fleet members whose checkpoint DB file is over
    ``checkpoint_alert_db_mb`` — by file size only (``stat``), never opening a member's
    live DB. Each member also banners its own per-thread detail."""
    db_mb = int(getattr(config, "checkpoint_alert_db_mb", 0) or 0) if config is not None else 0
    if db_mb <= 0:
        return None
    try:
        from graph.workspaces import manager

        over = []
        for ws in manager.list_workspaces():
            size = file_bytes(os.path.join(ws["path"], "checkpoints.db"))
            if size > db_mb * _MB:
                over.append((size, ws.get("label") or ws.get("name") or ws["id"]))
    except Exception:  # noqa: BLE001 — a status-poll warning must never raise
        return None
    if not over:
        return None
    over.sort(reverse=True)
    listed = ", ".join(f"{name} ({_human(size)})" for size, name in over)
    return (
        f"{len(over)} fleet member(s) have a conversation-history DB over {_human(db_mb * _MB)}: {listed}. "
        "Open the member for the largest threads; reclaim with "
        "`protoagent checkpoint compact --workspace <id>` (dry run by default; ADR 0120)."
    )
