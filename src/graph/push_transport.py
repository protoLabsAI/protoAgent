"""Bounded per-device delivery attempts, not a notice store (ADR 0135 W2).

Register ``deliver`` with the notice seam and periodically call ``drain`` (W2b4).
Only transport metadata is retained: no notice text, target, or binding secrets.
Accepted/terminal rows remain until expiry to suppress replay. A crash during a
send leaves an inflight row quarantined until expiry: service acceptance cannot
be inferred after restart. This is not an exactly-once presentation guarantee.
"""
from __future__ import annotations

import asyncio
import math
import random
import sqlite3
import time
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Callable, Literal, Protocol
from urllib.parse import quote

from graph.plugins.chat_surface import ChatNotice
from infra.paths import instance_paths
from security import devices
from security.devices import PushBinding


@dataclass(frozen=True)
class PushResult:
    status: Literal["accepted", "retry", "gone"]
    retry_after: float = 0


class PushProvider(Protocol):
    async def send(self, binding: PushBinding, payload: dict) -> PushResult:
        """Return service acceptance, proven retryable failure, or 404/410 gone.

        Payload expires_at bounds provider TTL (also capped at 24 hours).
        Exceptions/timeouts are ambiguous and quarantined, not blindly retried.
        """
        ...


class PushTransport:
    """Durable fanout with atomic claims, usable by concurrent drains/instances.

    ``eligible`` can consult authoritative decision state before each attempt;
    false discards obsolete prompts without persisting a second notice record.
    Queue overflow raises visibly before any part of that fanout is inserted.
    """

    def __init__(
        self, provider: PushProvider, *, clock: Callable[[], float] = time.time,
        jitter: Callable[[], float] = random.random, max_attempts: int = 5,
        capacity: int = 10000, concurrency: int = 8, timeout: float = 15,
        eligible: Callable[[str], bool] = lambda notice_id: True,
    ):
        if not 1 <= max_attempts <= 5 or capacity < 1 or concurrency < 1:
            raise ValueError("invalid push delivery bounds")
        if not math.isfinite(timeout) or timeout <= 0:
            raise ValueError("timeout must be positive and finite")
        self.provider = provider
        self.clock, self.jitter, self.eligible = clock, jitter, eligible
        self.max_attempts, self.capacity, self.timeout = max_attempts, capacity, timeout
        self._slots = asyncio.Semaphore(concurrency)
        self.path = instance_paths().store("push") / "delivery.sqlite3"
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with self._db() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS attempts (
                notice_id TEXT NOT NULL, device_id TEXT NOT NULL, generation INTEGER NOT NULL,
                kind TEXT NOT NULL, importance TEXT NOT NULL, created_at REAL NOT NULL,
                expires_at REAL NOT NULL, attempts INTEGER NOT NULL DEFAULT 0,
                next_at REAL NOT NULL, state TEXT NOT NULL DEFAULT 'pending',
                PRIMARY KEY (notice_id, device_id, generation))""")
            db.execute("DELETE FROM attempts WHERE expires_at <= ?", (self.clock(),))
        self.path.chmod(0o600)

    @contextmanager
    def _db(self):
        db = sqlite3.connect(self.path, timeout=5)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def enqueue(self, notice: ChatNotice) -> int:
        """Snapshot enrolled recipients once; repeated enqueue is idempotent per key."""
        if not notice.notice_id or not math.isfinite(notice.expires_at):
            raise ValueError("push requires a notice identity and finite expiry")
        now = self.clock()
        if notice.expires_at <= now:
            return 0
        recipients = []
        for view in devices.list_devices():
            device = devices.get_device(view["id"])
            if device and device.kind == devices.KIND_DEVICE and not device.is_expired(now) and device.push:
                recipients.append(device)
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("DELETE FROM attempts WHERE expires_at <= ?", (now,))
            added = 0
            for device in recipients:
                added += db.execute(
                    """INSERT OR IGNORE INTO attempts
                    (notice_id, device_id, generation, kind, importance, created_at, expires_at, next_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                    (notice.notice_id, device.id, device.push.generation, notice.kind,
                     notice.importance, notice.created_at, notice.expires_at, now),
                ).rowcount
            if db.execute("SELECT count(*) FROM attempts").fetchone()[0] > self.capacity:
                raise OverflowError("push delivery ledger capacity reached")
        return added

    async def deliver(self, notice: ChatNotice) -> None:
        """Notice-adapter entrypoint; enqueue durably before attempting delivery."""
        self.enqueue(notice)
        await self.drain()

    async def drain(self) -> None:
        """Send due rows independently; pending retries survive reconstruction."""
        with self._db() as db:
            db.execute("DELETE FROM attempts WHERE expires_at <= ?", (self.clock(),))
            rows = db.execute(
                "SELECT * FROM attempts WHERE state = 'pending' AND next_at <= ?", (self.clock(),)
            ).fetchall()
        await asyncio.gather(*(self._send(row) for row in rows))

    async def _send(self, row) -> None:
        async with self._slots:
            key = (row["notice_id"], row["device_id"], row["generation"])
            where = "notice_id = ? AND device_id = ? AND generation = ?"
            now = self.clock()
            device = devices.get_device(row["device_id"])
            if (row["expires_at"] <= now or device is None or device.is_expired(now)
                    or device.kind != devices.KIND_DEVICE or device.push is None
                    or device.push.generation != row["generation"]):
                with self._db() as db:
                    db.execute(f"DELETE FROM attempts WHERE {where}", key)
                return
            try:
                eligible = self.eligible(row["notice_id"])
            except Exception:
                return  # failed authoritative read: defer, never send stale state
            with self._db() as db:
                if not eligible:
                    db.execute(f"UPDATE attempts SET state = 'obsolete' WHERE {where}", key)
                    return
                claimed = db.execute(
                    f"""UPDATE attempts SET state = 'inflight', attempts = attempts + 1
                    WHERE {where} AND state = 'pending' AND next_at <= ? AND attempts < ?
                    AND attempts = ?""",
                    (*key, now, self.max_attempts, row["attempts"]),
                ).rowcount
            if not claimed:
                return
            payload = {
                "version": 1, "notice_id": row["notice_id"], "kind": row["kind"],
                "importance": row["importance"], "created_at": row["created_at"],
                "expires_at": row["expires_at"], "title": "protoAgent needs your attention",
                "body": "Open the console to view an update",
                "link": "/app/?notice_id=" + quote(row["notice_id"], safe=""),
            }
            try:
                result = await asyncio.wait_for(self.provider.send(device.push, payload), self.timeout)
            except Exception:
                return  # ambiguous acceptance; durable inflight claim quarantines it
            state, next_at = "failed", now
            if result.status == "gone":
                devices.invalidate_push_binding(device.id, row["generation"])
                state = "gone"
            elif result.status == "accepted":
                state = "accepted"
            elif result.status == "retry" and row["attempts"] + 1 < self.max_attempts:
                delay = min(3600, 30 * 2 ** row["attempts"] * (1 + self.jitter()))
                next_at = self.clock() + max(delay, result.retry_after)
                if math.isfinite(next_at) and next_at < row["expires_at"]:
                    state = "pending"
            with self._db() as db:
                db.execute(f"UPDATE attempts SET state = ?, next_at = ? WHERE {where}", (state, next_at, *key))
