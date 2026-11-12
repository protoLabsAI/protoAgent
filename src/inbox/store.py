"""InboxStore — a durable SQLite inbox for inbound stimuli (ADR 0003).

External systems (webhooks, cron, scripts, sister agents) push messages here via
``POST /api/inbox``. Each item carries a priority tier that governs delivery:

- ``now``   — surfaced immediately (the server fires an Activity turn).
- ``next``  — queued; the agent pulls it on its next ``check_inbox`` call.
- ``later`` — background; only returned on an explicit ``later`` floor.

An item may also carry ``delivery="thread"`` (ADR 0003 amendment, outside messages): it
never fires a turn of its own. The operator's live chat picks it up at its next model
call (``graph/middleware/outside_messages.py``), so a note from another session lands
in the thread the operator is actually reading.

Delivery decisions stay with the agent — the store just holds the material and
tracks what's been delivered. ``dedup_key`` collapses repeated posts (a webhook
that retries) within a window so they don't each fire a turn.
"""

from __future__ import annotations

import sqlite3
import threading
from collections import deque
from datetime import UTC, datetime, timedelta
from pathlib import Path

PRIORITIES = ("now", "next", "later")
#: How an item reaches the agent. ``""`` is the tiered behaviour above; ``"thread"`` is
#: folded into the operator's live chat at its next model call.
DELIVERY_THREAD = "thread"
DELIVERIES = ("", DELIVERY_THREAD)
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
        # In-process hint so the per-model-call drain costs nothing on the common path: WHO
        # may have an undelivered thread item waiting. ``""`` stands for an unaddressed
        # message (any operator chat may take it); any other entry is the one chat thread a
        # message names. Keyed by target so a message addressed to a thread that never
        # shows up (a typo, a closed tab) costs the OTHER chats nothing. Rebuilt from the
        # table at boot, added to on every add, replaced by each claim's own snapshot.
        # ``_hint_gen`` lets a claim notice an add that landed while it was running and
        # keep that add's entry instead of overwriting it with a stale snapshot.
        self._hint_lock = threading.Lock()
        self._hint_gen = 0
        self._thread_targets: set[str] = self._pending_thread_targets()

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
                    recovery_accepted_at TEXT,
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
            # Durable "delivery was accepted" marker (#3351 review). Survives restart so an
            # accepted page whose mark_delivered write failed is COMPLETED on the next
            # recovery pass (marked delivered) rather than re-fired — the in-process claim
            # alone could not carry that across a boot, which reset_stale_recovery_claims clears.
            if "recovery_accepted_at" not in existing:
                db.execute("ALTER TABLE inbox ADD COLUMN recovery_accepted_at TEXT")
            if "recovery_error" not in existing:
                db.execute("ALTER TABLE inbox ADD COLUMN recovery_error TEXT")
            if "recovery_attempts" not in existing:
                db.execute("ALTER TABLE inbox ADD COLUMN recovery_attempts INTEGER NOT NULL DEFAULT 0")
            # Outside messages: how the item is delivered, and the chat that read it.
            if "delivery" not in existing:
                db.execute("ALTER TABLE inbox ADD COLUMN delivery TEXT NOT NULL DEFAULT ''")
            if "delivered_session" not in existing:
                db.execute("ALTER TABLE inbox ADD COLUMN delivered_session TEXT")
            # A direct message may name ONE chat thread; only that thread reads it.
            if "target_session" not in existing:
                db.execute("ALTER TABLE inbox ADD COLUMN target_session TEXT")
            # A thread claim is only FINAL once the note is known to be in the chat's state
            # (acked_at). A claim with no ack is handed out again at the next boot.
            if "acked_at" not in existing:
                db.execute("ALTER TABLE inbox ADD COLUMN acked_at TEXT")
            # The agent's answer to a direct message (reply_to_dm), for the sender to read.
            if "reply_text" not in existing:
                db.execute("ALTER TABLE inbox ADD COLUMN reply_text TEXT")
            if "replied_at" not in existing:
                db.execute("ALTER TABLE inbox ADD COLUMN replied_at TEXT")
            db.execute("CREATE INDEX IF NOT EXISTS ix_inbox_undelivered ON inbox(delivered_at, priority)")
            # add() looks a dedup_key up inside its write transaction; keep that a seek.
            db.execute("CREATE INDEX IF NOT EXISTS ix_inbox_dedup ON inbox(dedup_key, delivered_at)")
            # A thread claim that never reached the chat (the process died between the
            # claim's commit and the note landing in state) is released here, once, at
            # boot: delivery is at-least-once, never silently lost.
            db.execute(
                "UPDATE inbox SET delivered_at = NULL, delivered_session = NULL "
                "WHERE delivery = 'thread' AND delivered_at IS NOT NULL AND acked_at IS NULL "
                "AND delivered_session IS NOT NULL AND reply_text IS NULL"
            )
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
        delivery: str = "",
        target_session: str = "",
        now: datetime | None = None,
    ) -> dict | None:
        """Insert an item. Returns the row, or ``None`` if deduped.

        Raises ``ValueError`` on empty text, an unknown priority or an unknown delivery.
        """
        text = (text or "").strip()
        if not text:
            raise ValueError("inbox item text is empty")
        if priority not in PRIORITIES:
            raise ValueError(f"unknown priority {priority!r} (expected one of {PRIORITIES})")
        delivery = (delivery or "").strip()
        if delivery not in DELIVERIES:
            raise ValueError(f"unknown delivery {delivery!r} (expected '' or {DELIVERY_THREAD!r})")
        target_session = (target_session or "").strip()
        if target_session and delivery != DELIVERY_THREAD:
            raise ValueError("only a direct message (thread delivery) can name a chat thread")
        now = now or datetime.now(UTC)

        db = self._connect()
        try:
            # The dedup check and the insert are one write transaction: two posts of the
            # same webhook retry arriving together cannot both pass the check.
            db.execute("BEGIN IMMEDIATE")
            if dedup_key:
                cutoff = (now - timedelta(seconds=self._dedup_window_s)).isoformat()
                dup = db.execute(
                    "SELECT id FROM inbox WHERE dedup_key = ? AND delivered_at IS NULL AND created_at >= ? LIMIT 1",
                    (dedup_key, cutoff),
                ).fetchone()
                if dup is not None:
                    db.rollback()
                    return None
            cur = db.execute(
                "INSERT INTO inbox (created_at, priority, source, text, dedup_key, delivery, target_session) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (now.isoformat(), priority, source, text, dedup_key or None, delivery, target_session or None),
            )
            db.commit()
            row = db.execute("SELECT * FROM inbox WHERE id = ?", (cur.lastrowid,)).fetchone()
            if delivery == DELIVERY_THREAD:
                with self._hint_lock:
                    self._hint_gen += 1
                    self._thread_targets.add(target_session)
            return dict(row)
        finally:
            db.close()

    def get(self, item_id: int) -> dict | None:
        """One item by id (delivered or not), or ``None``."""
        db = self._connect()
        try:
            row = db.execute("SELECT * FROM inbox WHERE id = ?", (int(item_id),)).fetchone()
            return dict(row) if row is not None else None
        finally:
            db.close()

    def reply(self, item_id: int, text: str, *, now: datetime | None = None) -> dict:
        """Record the agent's answer to a direct message, for the sender to read.

        Only a thread item that a chat has already read can be answered. A second reply is
        appended, never overwritten: the sender may have read the first one already.
        Raises ``ValueError`` (the caller's mistake, with a message it can act on).
        """
        text = (text or "").strip()
        if not text:
            raise ValueError("the reply is empty")
        stamp = (now or datetime.now(UTC)).isoformat()
        db = self._connect()
        try:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM inbox WHERE id = ?", (int(item_id),)).fetchone()
            if row is None:
                db.rollback()
                raise ValueError(f"no direct message with id {item_id}")
            if row["delivery"] != DELIVERY_THREAD:
                db.rollback()
                raise ValueError(f"inbox item {item_id} is not a direct message, so nobody is waiting on a reply")
            # A chat's claim is what counts as "read": the console's dismiss control also
            # sets delivered_at, with no session, and nobody read the message then.
            if row["delivered_at"] is None or not row["delivered_session"]:
                db.rollback()
                raise ValueError(f"direct message {item_id} has not been read by a chat yet")
            combined = f"{row['reply_text']}\n\n{text}" if row["reply_text"] else text
            db.execute("UPDATE inbox SET reply_text = ?, replied_at = ? WHERE id = ?", (combined, stamp, int(item_id)))
            db.commit()
            return {**dict(row), "reply_text": combined, "replied_at": stamp}
        finally:
            db.close()

    @staticmethod
    def _targets_in(db: sqlite3.Connection) -> set[str]:
        rows = db.execute(
            "SELECT DISTINCT COALESCE(target_session, '') AS t FROM inbox WHERE delivery = ? AND delivered_at IS NULL",
            (DELIVERY_THREAD,),
        ).fetchall()
        return {r["t"] for r in rows}

    def _pending_thread_targets(self) -> set[str]:
        db = self._connect()
        try:
            return self._targets_in(db)
        finally:
            db.close()

    def thread_maybe_pending(self, session_id: str | None = None) -> bool:
        """Cheap, SQLite-free hint. With a ``session_id``: False means there is certainly
        nothing for THAT chat to claim (no unaddressed message, none addressed to it).
        Without one: whether any thread message is waiting for anybody."""
        targets = self._thread_targets
        if session_id is None:
            return bool(targets)
        return "" in targets or session_id in targets

    def ack_thread_items(self, ids: list[int], *, now: datetime | None = None) -> int:
        """Make a thread claim final: the note is in the chat's state. Idempotent."""
        if not ids:
            return 0
        stamp = (now or datetime.now(UTC)).isoformat()
        db = self._connect()
        try:
            placeholders = ",".join("?" for _ in ids)
            cur = db.execute(
                f"UPDATE inbox SET acked_at = ? WHERE id IN ({placeholders}) AND acked_at IS NULL "
                "AND delivered_at IS NOT NULL",
                (stamp, *ids),
            )
            db.commit()
            return cur.rowcount
        finally:
            db.close()

    def release_thread_items(self, ids: list[int]) -> int:
        """Undo a thread claim whose note never reached the chat (the turn was cancelled or
        failed between the claim and the injection), so a later model call reads it."""
        if not ids:
            return 0
        db = self._connect()
        try:
            db.execute("BEGIN IMMEDIATE")
            placeholders = ",".join("?" for _ in ids)
            cur = db.execute(
                "UPDATE inbox SET delivered_at = NULL, delivered_session = NULL "
                f"WHERE id IN ({placeholders}) AND delivery = ? AND acked_at IS NULL AND delivered_at IS NOT NULL",
                (*ids, DELIVERY_THREAD),
            )
            snapshot = self._targets_in(db)
            db.commit()
            with self._hint_lock:
                self._hint_gen += 1
                self._thread_targets = self._thread_targets | snapshot
            return cur.rowcount
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    def claim_thread_items(self, session_id: str, *, limit: int = 20, now: datetime | None = None) -> list[dict]:
        """Atomically take the undelivered thread items for ``session_id``, oldest first.

        A message addressed to one thread (``target_session``) is only ever taken by that
        thread; an unaddressed one goes to whichever operator chat reaches a model call first.

        Each item is handed to exactly one chat: the select and the mark-delivered run in
        one ``BEGIN IMMEDIATE`` transaction, so two chats reaching a model call together
        cannot both read the same note. The claimed rows record which chat read them.

        A claim is provisional until ``ack_thread_items``: the caller acks once the note is
        in the chat's state, or ``release_thread_items`` if it could not inject it.
        """
        stamp = (now or datetime.now(UTC)).isoformat()
        with self._hint_lock:
            gen = self._hint_gen
        db = self._connect()
        try:
            db.execute("BEGIN IMMEDIATE")
            rows = db.execute(
                "SELECT * FROM inbox WHERE delivery = ? AND delivered_at IS NULL "
                "AND (target_session IS NULL OR target_session = '' OR target_session = ?) "
                "ORDER BY created_at ASC, id ASC LIMIT ?",
                (DELIVERY_THREAD, session_id, max(1, int(limit))),
            ).fetchall()
            if rows:
                ids = [r["id"] for r in rows]
                placeholders = ",".join("?" for _ in ids)
                db.execute(
                    f"UPDATE inbox SET delivered_at = ?, delivered_session = ? WHERE id IN ({placeholders})",
                    (stamp, session_id, *ids),
                )
            # Who is still waiting, read inside the same transaction as the claim.
            snapshot = self._targets_in(db)
            db.commit()
            with self._hint_lock:
                if self._hint_gen == gen:
                    self._thread_targets = snapshot
                else:
                    # An add landed while this claim ran; its row may be newer than the
                    # snapshot. Keep its entry: a spare lookup is cheap, a stranded
                    # message is not.
                    self._thread_targets = self._thread_targets | snapshot
            return [{**dict(r), "delivered_at": stamp, "delivered_session": session_id} for r in rows]
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    def list(
        self,
        *,
        priority_floor: str = "next",
        include_delivered: bool = False,
        include_recovery_claimed: bool = False,
        include_thread: bool = False,
        limit: int = 20,
    ) -> list[dict]:
        tiers = _floor_set(priority_floor)
        placeholders = ",".join("?" for _ in tiers)
        where = f"priority IN ({placeholders})"
        params = tuple(tiers)
        if not include_thread:
            # Thread items belong to the operator's live chat (claim_thread_items). A
            # check_inbox pull from some other turn must not swallow them first.
            where += f" AND delivery != '{DELIVERY_THREAD}'"
        if not include_delivered:
            where += " AND delivered_at IS NULL"
            if not include_recovery_claimed:
                # Hide a now page that recovery is holding: either an in-flight claim, OR a
                # durable accepted-but-unmarked delivery (recovery_accepted_at). The latter
                # was already delivered to the operator; surfacing it to a check_inbox pull
                # would fire a duplicate Activity turn even after the ephemeral claim is
                # cleared at startup. Recovery completes it (marks delivered) without re-firing.
                where += (
                    " AND NOT (priority = 'now' AND "
                    "(recovery_claimed_at IS NOT NULL OR recovery_accepted_at IS NOT NULL))"
                )
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
                "UPDATE inbox SET delivered_at = NULL, recovery_claimed_at = NULL, "
                "recovery_accepted_at = NULL "
                # Only un-deliver what IS delivered (the mirror of mark_delivered's guard):
                # an undelivered item mid-recovery must keep its recovery claim.
                f"WHERE id IN ({placeholders}) AND delivered_at IS NOT NULL",
                tuple(ids),
            )
            snapshot = self._targets_in(db)
            db.commit()
            with self._hint_lock:
                self._hint_gen += 1
                self._thread_targets = self._thread_targets | snapshot
            return cur.rowcount
        finally:
            db.close()

    def reset_stale_recovery_claims(
        self,
        *,
        retry_after_s: int = 3600,
        now: datetime | None = None,
    ) -> int:
        """Clear prior-process in-flight recovery claims. Call ONCE at startup,
        before the recovery pass, and nowhere else.

        The claim (``recovery_claimed_at``) is an in-process lease: it stops a concurrent
        ``check_inbox`` pull from grabbing a page while recovery awaits its Activity turn.
        A lease is therefore only meaningful to the process that took it — so any claim
        still present at startup may have been taken by a process that no longer exists.
        Holding it hides the page from pull fallback (``list`` skips claimed ``now`` rows),
        so startup releases all claims regardless of wall-clock age.

        This deliberately does NOT reset ``recovery_attempts``: restart replay is bounded
        by attempt count, not by a clock, so crashed pages are immediately recoverable
        while permanently refused pages still stop replaying.
        """
        _ = retry_after_s, now
        db = self._connect()
        try:
            cur = db.execute(
                """
                UPDATE inbox
                SET recovery_claimed_at = NULL
                WHERE recovery_claimed_at IS NOT NULL
                """
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

        **Delivery is AT-LEAST-ONCE only across a genuine crash.** If the process dies
        between the fire and any durable record, no reader can distinguish an accepted page
        from a claim that never fired — without a two-phase commit against the Activity path
        that is unavoidable, and this errs toward delivering a page twice rather than losing
        it (a duplicate is one extra Activity line; a lost page is the whole of #3351, which
        stranded 29 operator alerts for four days).

        But when the process SURVIVES a ``mark_delivered`` failure it DOES know delivery was
        accepted, and :meth:`record_recovery_mark_delivered_failure` persists that as a
        durable ``recovery_accepted_at`` marker. Such a row stays undelivered but is hidden
        from the ``check_inbox`` pull fallback (``list``) and is claimed here for COMPLETION
        only: the recovery caller marks it delivered without re-firing, so a restart cannot
        turn a recorded mark-failure into a duplicate Activity turn. A caller that needs
        exactly-once against the unrecorded-crash case must still dedupe downstream.

        Storm protection is the bounded ``recovery_attempts`` count: a page that keeps
        refusing delivery stops after ``max_attempts`` and stays visible to the
        ``check_inbox`` pull fallback while unclaimed. An accepted-but-unmarked page
        (``recovery_accepted_at`` set) is exempt from that bound — completing it marks it
        delivered and fires no turn, so it can never contribute to a replay storm and must
        never be stranded by the attempt budget. ``retry_after_s`` is retained for
        compatibility with callers from the first slice, but recovery no longer gates
        crash recoverability on wall-clock age.

        ``BEGIN IMMEDIATE`` takes the write lock before the candidate SELECT, so no other
        writer can deliver one of these rows between our read and our claim UPDATE.
        """
        now = now or datetime.now(UTC)
        now_iso = now.isoformat()
        _ = retry_after_s
        batch_limit = max(1, int(limit))
        db = self._connect()
        try:
            db.execute("BEGIN IMMEDIATE")
            rows = db.execute(
                """
                SELECT * FROM inbox
                WHERE priority = 'now'
                  AND delivered_at IS NULL
                  AND recovery_claimed_at IS NULL
                  AND (recovery_accepted_at IS NOT NULL OR recovery_attempts < ?)
                ORDER BY created_at ASC
                LIMIT ?
                """,
                (max_attempts, batch_limit),
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

        Persists a **durable** ``recovery_accepted_at`` marker (kept at its earliest value
        via COALESCE). This is the accepted-delivery state the #3351 review found missing:
        once the Activity turn has been accepted, this marker survives restart, so the next
        recovery pass COMPLETES the page (marks it delivered) instead of re-firing it, and
        ``list`` hides it from the pull fallback in the meantime. The in-process
        ``recovery_claimed_at`` alone could not carry that intent across a boot —
        ``reset_stale_recovery_claims`` clears every claim at startup — which is exactly how
        an accepted page became eligible for redelivery again.

        ``recovery_claimed_at`` is deliberately left in place too, so the page is also hidden
        within the current process before any restart. The row stays undelivered with
        operator-readable evidence rather than being marked delivered against a write that
        did not land.
        """
        now = now or datetime.now(UTC)
        detail = (reason or "accepted delivery could not be marked delivered").strip()[:500]
        where = "id = ? AND delivered_at IS NULL"
        params: list = [now.isoformat(), now.isoformat(), detail, int(item_id)]
        if claimed_at is not None:
            where += " AND recovery_claimed_at = ?"
            params.append(claimed_at)
        db = self._connect()
        try:
            cur = db.execute(
                f"""
                UPDATE inbox
                SET recovery_accepted_at = COALESCE(recovery_accepted_at, ?),
                    recovery_attempted_at = ?,
                    recovery_error = ?
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
