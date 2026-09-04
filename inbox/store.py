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
        # busy_timeout FIRST: the WAL pragma itself takes a lock, so setting the
        # timeout after it leaves an unguarded window that raises "database is
        # locked" under contention (#2428 — wide enough to flake on Windows).
        db.execute("PRAGMA busy_timeout=5000")  # wait (don't error) on lock contention
        db.execute("PRAGMA journal_mode=WAL")  # concurrent reads during writes
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
                    delivered_at TEXT,
                    recovery_claimed_at TEXT,
                    recovery_attempted_at TEXT,
                    recovery_error TEXT,
                    recovery_attempts INTEGER NOT NULL DEFAULT 0
                )
                """
            )
            existing = {r["name"] for r in db.execute("PRAGMA table_info(inbox)").fetchall()}
            if "recovery_claimed_at" not in existing:
                db.execute("ALTER TABLE inbox ADD COLUMN recovery_claimed_at TEXT")
            if "recovery_attempted_at" not in existing:
                db.execute("ALTER TABLE inbox ADD COLUMN recovery_attempted_at TEXT")
            if "recovery_error" not in existing:
                db.execute("ALTER TABLE inbox ADD COLUMN recovery_error TEXT")
            if "recovery_attempts" not in existing:
                db.execute("ALTER TABLE inbox ADD COLUMN recovery_attempts INTEGER NOT NULL DEFAULT 0")
            db.execute("CREATE INDEX IF NOT EXISTS ix_inbox_undelivered ON inbox(delivered_at, priority)")
            db.execute(
                "CREATE INDEX IF NOT EXISTS ix_inbox_now_recovery "
                "ON inbox(priority, delivered_at, recovery_claimed_at, recovery_attempted_at)"
            )
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
        include_recovery_claimed: bool = False,
        limit: int = 20,
    ) -> list[dict]:
        tiers = _floor_set(priority_floor)
        placeholders = ",".join("?" for _ in tiers)
        where = f"priority IN ({placeholders})"
        params = tuple(tiers)
        if not include_delivered:
            where += " AND delivered_at IS NULL"
            if not include_recovery_claimed:
                where += " AND NOT (priority = 'now' AND recovery_claimed_at IS NOT NULL)"
        db = self._connect()
        try:
            rows = db.execute(
                f"SELECT * FROM inbox WHERE {where} "
                "ORDER BY CASE priority WHEN 'now' THEN 0 WHEN 'next' THEN 1 ELSE 2 END, created_at ASC "
                "LIMIT ?",
                (*params, max(1, int(limit))),
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
                "UPDATE inbox SET delivered_at = ?, recovery_claimed_at = NULL, recovery_error = NULL "
                f"WHERE id IN ({placeholders}) AND delivered_at IS NULL",
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
                "UPDATE inbox SET delivered_at = NULL, recovery_claimed_at = NULL "
                f"WHERE id IN ({placeholders})",
                tuple(ids),
            )
            db.commit()
            return cur.rowcount
        finally:
            db.close()

    def reset_stale_recovery_claims(self) -> int:
        """Clear every in-flight recovery claim. Call ONCE at startup, before the recovery
        pass, and nowhere else.

        The claim (``recovery_claimed_at``) is an in-process lease: it stops a concurrent
        ``check_inbox`` pull from grabbing a page while recovery awaits its Activity turn.
        A lease is therefore only meaningful to the process that took it — so any claim
        still present at startup was taken by a process that no longer exists, and holding
        it hides the page from BOTH recovery (the selector skips claimed rows) and the pull
        fallback (``list`` skips claimed ``now`` rows) until the lease ages out.

        Without this, a crash between the claim and the deliver/restore handoff made an
        operator page invisible for the whole ``retry_after_s`` window, and a replacement
        process that booted inside that window could not reclaim it at all — the page just
        went quiet. Recoverability must not depend on wall-clock; storm protection is
        ``recovery_attempts``, which this deliberately does NOT reset.
        """
        db = self._connect()
        try:
            cur = db.execute(
                "UPDATE inbox SET recovery_claimed_at = NULL WHERE recovery_claimed_at IS NOT NULL"
            )
            db.commit()
            return cur.rowcount
        finally:
            db.close()

    def claim_now_recovery_batch(
        self,
        *,
        limit: int = 8,
        retry_after_s: int = 3600,
        max_attempts: int = 5,
        now: datetime | None = None,
    ) -> list[dict]:
        """Atomically claim a bounded batch of pre-existing pending ``now`` items for
        boot recovery, returning the claimed rows.

        Claimed rows remain undelivered until accepted delivery marks them delivered.
        ``recovery_claimed_at`` is a bounded in-flight lease that keeps a concurrent
        inbox consumer (a fresh now-post or a ``check_inbox`` pull) from grabbing the
        same page while recovery awaits its Activity turn. Callers MUST hand every
        claimed item that does NOT achieve accepted delivery back to
        :meth:`restore_recovery_failure`, which clears the claim so pull fallback can
        still pick it up. If the process dies before that restore/deliver handoff, a
        later startup pass may reclaim the stale lease after ``retry_after_s``.

        **Delivery is AT-LEAST-ONCE, by decision.** The claim commits before the fire and
        ``mark_delivered`` commits after it, so a crash or a failed mark between the two is
        indistinguishable at read time from a claim that never fired — no reader can tell
        them apart without a two-phase commit against the Activity path. Faced with that,
        this errs toward delivering a page twice rather than losing it: a duplicate is one
        extra line in an Activity feed, while a lost page is the whole of #3351, which
        stranded 29 operator alerts for four days. A caller that needs exactly-once must
        dedupe downstream; do not add a test pinning exactly-once here, because the design
        cannot honour it.

        Storm protection is ``recovery_attempts``, a bounded counter incremented on every
        claim: a page that keeps refusing delivery stops being retried after
        ``max_attempts`` and stays visible to the ``check_inbox`` pull instead. It is a
        COUNT, not a clock, so a crashed page is eligible again on the next boot rather
        than after ``retry_after_s`` — recoverability and throttling are separate concerns
        and were previously conflated in one wall-clock gate.

        ``BEGIN IMMEDIATE`` takes the write lock before the candidate SELECT, so no other
        writer can deliver one of these rows between our read and our claim UPDATE.
        """
        now = now or datetime.now(UTC)
        now_iso = now.isoformat()
        cutoff = (now - timedelta(seconds=max(0, int(retry_after_s)))).isoformat()
        batch_limit = max(1, int(limit))
        db = self._connect()
        try:
            db.execute("BEGIN IMMEDIATE")
            rows = db.execute(
                """
                SELECT * FROM inbox
                WHERE priority = 'now'
                  AND delivered_at IS NULL
                  AND (recovery_claimed_at IS NULL OR recovery_claimed_at < ?)
                  AND recovery_attempts < ?
                ORDER BY created_at ASC
                LIMIT ?
                """,
                (cutoff, max_attempts, batch_limit),
            ).fetchall()
            ids = [int(r["id"]) for r in rows]
            if ids:
                placeholders = ",".join("?" for _ in ids)
                db.execute(
                    "UPDATE inbox SET recovery_claimed_at = ?, recovery_attempted_at = ?, recovery_error = NULL, "
                    "recovery_attempts = recovery_attempts + 1 "
                    f"WHERE id IN ({placeholders}) AND delivered_at IS NULL",
                    (now_iso, now_iso, *ids),
                )
            db.commit()
            # Reflect the claim we just wrote onto the returned rows: the SELECT above is a
            # pre-UPDATE snapshot, so its recovery_claimed_at is stale. Callers pass this
            # exact stamp back to restore_recovery_failure(claimed_at=...) so a late failure
            # only restores a row that STILL bears this recovery's claim.
            claimed = []
            for r in rows:
                d = dict(r)
                d["recovery_claimed_at"] = now_iso
                d["recovery_attempted_at"] = now_iso
                d["recovery_error"] = None
                claimed.append(d)
            return claimed
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    def restore_recovery_failure(
        self,
        item_id: int,
        reason: str,
        *,
        claimed_at: str | None = None,
        now: datetime | None = None,
    ) -> int:
        """Restore a claimed-but-unfired recovery item to pending with bounded,
        operator-readable failure evidence.

        Clears the in-flight claim so ``check_inbox`` pull fallback can pick the page
        up, while keeping ``recovery_attempted_at`` set so the next restart backs off
        (``retry_after_s``) instead of replaying it immediately.
        The inverse landing spot for a :meth:`claim_now_recovery_batch` claim whose
        delivery was not accepted.

        Guarded so a *late* recovery failure can never reopen a page that has since been
        handled by someone else. The restore therefore only touches the row while it
        is still undelivered (``delivered_at IS NULL``) and, when ``claimed_at`` is given,
        still bears *this* recovery's claim (``recovery_claimed_at = claimed_at``). If the
        row was delivered in the meantime this is a no-op (returns 0): a straggling
        failure cannot un-deliver a delivered item. ``delivered_at`` is left untouched —
        the undelivered guard makes clearing it unnecessary and never risks reopening."""
        now = now or datetime.now(UTC)
        detail = (reason or "delivery was not accepted").strip()[:500]
        where = "id = ? AND delivered_at IS NULL"
        params: list = [now.isoformat(), detail, int(item_id)]
        if claimed_at is not None:
            where += " AND recovery_claimed_at = ?"
            params.append(claimed_at)
        db = self._connect()
        try:
            cur = db.execute(
                f"""
                UPDATE inbox
                SET recovery_claimed_at = NULL, recovery_attempted_at = ?, recovery_error = ?
                WHERE {where}
                """,
                tuple(params),
            )
            db.commit()
            return cur.rowcount
        finally:
            db.close()

    def record_recovery_mark_delivered_failure(
        self,
        item_id: int,
        reason: str,
        *,
        claimed_at: str | None = None,
        now: datetime | None = None,
    ) -> int:
        """Record that accepted delivery could not be marked delivered.

        This deliberately leaves ``recovery_claimed_at`` in place. Once the accepted
        delivery path has taken ownership of the Activity turn, clearing the claim would
        let a later pull redeliver the same page immediately. The row remains
        undelivered with operator-readable evidence; if the mark cannot be repaired by
        then, the bounded recovery lease can be retried after the retry window instead
        of hiding the page forever.
        """
        now = now or datetime.now(UTC)
        detail = (reason or "accepted delivery could not be marked delivered").strip()[:500]
        where = "id = ? AND delivered_at IS NULL"
        params: list = [now.isoformat(), detail, int(item_id)]
        if claimed_at is not None:
            where += " AND recovery_claimed_at = ?"
            params.append(claimed_at)
        db = self._connect()
        try:
            cur = db.execute(
                f"""
                UPDATE inbox
                SET recovery_attempted_at = ?, recovery_error = ?
                WHERE {where}
                """,
                tuple(params),
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
