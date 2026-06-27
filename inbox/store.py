"""InboxStore — a durable SQLite inbox for inbound stimuli (ADR 0003).

External systems (webhooks, cron, scripts, sister agents) push messages here via
``POST /api/inbox``. Each item carries a priority tier that governs delivery:

- ``now``   — surfaced immediately (the server fires an Activity turn).
- ``next``  — queued; the agent pulls it on its next ``check_inbox`` call.
- ``later`` — background; only returned on an explicit ``later`` floor.

Delivery decisions stay with the agent — the store just holds the material and
tracks what's been delivered. ``dedup_key`` collapses repeated posts (a webhook
that retries) within a window so they don't each fire a turn.
"""

from __future__ import annotations

import sqlite3
from collections import deque
from datetime import UTC, datetime, timedelta
from pathlib import Path

PRIORITIES = ("now", "next", "later")
_RANK = {"now": 0, "next": 1, "later": 2}


def _floor_set(priority_floor: str) -> tuple[str, ...]:
    """Tiers visible at a given floor: now→{now}, next→{now,next}, later→all."""
    cutoff = _RANK.get(priority_floor, 1)
    return tuple(p for p in PRIORITIES if _RANK[p] <= cutoff)


class InboxStore:
    def __init__(self, db_path: str, *, dedup_window_s: int = 300) -> None:
        self.path = str(db_path)
        self._dedup_window_s = dedup_window_s
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.path)
        db.execute("PRAGMA journal_mode=WAL")  # concurrent reads during writes
        db.execute("PRAGMA busy_timeout=5000")  # wait (don't error) on lock contention
        db.row_factory = sqlite3.Row
        return db

    def _init_db(self) -> None:
        db = self._connect()
        try:
            db.execute(
                """
                CREATE TABLE IF NOT EXISTS inbox (
                    id           INTEGER PRIMARY KEY AUTOINCREMENT,
                    created_at   TEXT NOT NULL,
                    priority     TEXT NOT NULL,
                    source       TEXT,
                    text         TEXT NOT NULL,
                    dedup_key    TEXT,
                    delivered_at TEXT
                )
                """
            )
            db.execute("CREATE INDEX IF NOT EXISTS ix_inbox_undelivered ON inbox(delivered_at, priority)")
            db.commit()
        finally:
            db.close()

    def add(
        self,
        text: str,
        *,
        priority: str = "next",
        source: str = "",
        dedup_key: str = "",
        now: datetime | None = None,
    ) -> dict | None:
        """Insert an item. Returns the row, or ``None`` if deduped.

        Raises ``ValueError`` on empty text or an unknown priority.
        """
        text = (text or "").strip()
        if not text:
            raise ValueError("inbox item text is empty")
        if priority not in PRIORITIES:
            raise ValueError(f"unknown priority {priority!r} (expected one of {PRIORITIES})")
        now = now or datetime.now(UTC)

        db = self._connect()
        try:
            if dedup_key:
                cutoff = (now - timedelta(seconds=self._dedup_window_s)).isoformat()
                dup = db.execute(
                    "SELECT id FROM inbox WHERE dedup_key = ? AND delivered_at IS NULL AND created_at >= ? LIMIT 1",
                    (dedup_key, cutoff),
                ).fetchone()
                if dup is not None:
                    return None
            cur = db.execute(
                "INSERT INTO inbox (created_at, priority, source, text, dedup_key) VALUES (?, ?, ?, ?, ?)",
                (now.isoformat(), priority, source, text, dedup_key or None),
            )
            db.commit()
            row = db.execute("SELECT * FROM inbox WHERE id = ?", (cur.lastrowid,)).fetchone()
            return dict(row)
        finally:
            db.close()

    def list(
        self,
        *,
        priority_floor: str = "next",
        include_delivered: bool = False,
        limit: int = 20,
    ) -> list[dict]:
        tiers = _floor_set(priority_floor)
        placeholders = ",".join("?" for _ in tiers)
        where = f"priority IN ({placeholders})"
        if not include_delivered:
            where += " AND delivered_at IS NULL"
        db = self._connect()
        try:
            rows = db.execute(
                f"SELECT * FROM inbox WHERE {where} "
                "ORDER BY CASE priority WHEN 'now' THEN 0 WHEN 'next' THEN 1 ELSE 2 END, created_at ASC "
                "LIMIT ?",
                (*tiers, max(1, int(limit))),
            ).fetchall()
            return [dict(r) for r in rows]
        finally:
            db.close()

    def mark_delivered(self, ids: list[int], *, now: datetime | None = None) -> int:
        if not ids:
            return 0
        now = now or datetime.now(UTC)
        db = self._connect()
        try:
            placeholders = ",".join("?" for _ in ids)
            cur = db.execute(
                f"UPDATE inbox SET delivered_at = ? WHERE id IN ({placeholders}) AND delivered_at IS NULL",
                (now.isoformat(), *ids),
            )
            db.commit()
            return cur.rowcount
        finally:
            db.close()

    def mark_pending(self, ids: list[int]) -> int:
        """Un-deliver: clear ``delivered_at`` so an item re-enters the pending queue. The
        inverse of ``mark_delivered`` — used to RESTORE a now-item that was optimistically
        delivered (so its fired turn couldn't double-read it) but whose fire then failed."""
        if not ids:
            return 0
        db = self._connect()
        try:
            placeholders = ",".join("?" for _ in ids)
            cur = db.execute(
                f"UPDATE inbox SET delivered_at = NULL WHERE id IN ({placeholders})",
                tuple(ids),
            )
            db.commit()
            return cur.rowcount
        finally:
            db.close()

    def pending_count(self, *, priority_floor: str = "next") -> int:
        return len(self.list(priority_floor=priority_floor, limit=1000))

    def prune(self, keep_days: int = 90, *, now: datetime | None = None) -> int:
        """Delete delivered items older than ``keep_days``. Pending items
        (undelivered) are never pruned. Returns rows removed. 0 = keep forever."""
        if keep_days == 0:
            return 0
        now = now or datetime.now(UTC)
        cutoff = (now - timedelta(days=keep_days)).isoformat()
        db = self._connect()
        try:
            cur = db.execute(
                "DELETE FROM inbox WHERE delivered_at IS NOT NULL AND created_at < ?",
                (cutoff,),
            )
            db.commit()
            return cur.rowcount
        finally:
            db.close()


class StormGuard:
    """Anti-storm rate limiter for the now→fire path.

    Allows at most ``max_fires`` within a rolling ``window_s`` second window.
    Once exceeded, ``allow`` returns ``False`` until the rate drops — so a
    misconfigured or hostile producer can't flood the agent with turns.
    """

    def __init__(self, *, max_fires: int = 8, window_s: float = 60.0) -> None:
        self._max = max_fires
        self._window_s = window_s
        self._fires: deque[float] = deque()

    def allow(self, now_ts: float) -> bool:
        cutoff = now_ts - self._window_s
        while self._fires and self._fires[0] < cutoff:
            self._fires.popleft()
        if len(self._fires) >= self._max:
            return False
        self._fires.append(now_ts)
        return True
