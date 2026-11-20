"""Durable quiet-hours hold at the shared notice delivery seam."""
from __future__ import annotations

import asyncio
import logging
import re
import sqlite3
import threading
import time
from contextlib import closing
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from infra.paths import instance_paths

log = logging.getLogger(__name__)
_invalid_logged = False


def quiet_until(config, now: float) -> float | None:
    """Return the first non-quiet instant, or None (including invalid settings).

    Equal endpoints mean an empty window. Walk UTC minutes to honor both skipped
    and repeated local times across daylight-saving transitions.
    """
    global _invalid_logged
    if not getattr(config, "quiet_hours_enabled", False):
        return None
    try:
        values = [config.quiet_hours_start, config.quiet_hours_end]
        if any(not isinstance(v, str) or not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", v) for v in values):
            raise ValueError("expected HH:MM")
        start, end = [int(v[:2]) * 60 + int(v[3:]) for v in values]
        zone = ZoneInfo(config.quiet_hours_timezone)
    except (ValueError, TypeError, ZoneInfoNotFoundError):
        if not _invalid_logged:
            log.warning("Invalid quiet hours time or timezone; delivering notices normally")
            _invalid_logged = True
        return None

    def inside(stamp):
        local = datetime.fromtimestamp(stamp, zone)
        minute = local.hour * 60 + local.minute
        return start <= minute < end if start <= end else minute >= start or minute < end

    if not inside(now):
        return None
    boundary = (int(now) // 60 + 1) * 60
    while inside(boundary):
        boundary += 60
    return float(boundary)


class NoticeHold:
    """Short-lived connections keep the SQLite hold usable across restarts/threads."""

    def __init__(self, path: Path | None = None):
        self.path = path or instance_paths().store("notices") / "hold.sqlite3"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute("CREATE TABLE IF NOT EXISTS held (id INTEGER PRIMARY KEY, text TEXT NOT NULL)")

    def add(self, text: str) -> None:
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute("INSERT INTO held(text) VALUES (?)", (text,))

    def rows(self) -> list[tuple[int, str]]:
        with closing(sqlite3.connect(self.path)) as db:
            return db.execute("SELECT id, text FROM held ORDER BY id").fetchall()

    def clear_through(self, row_id: int) -> None:
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute("DELETE FROM held WHERE id <= ?", (row_id,))


class QuietHoursDelivery:
    """Serialize holds and digest flushes; own one timer on the server loop."""

    def __init__(self):
        self.lock = threading.RLock()
        self.store: NoticeHold | None = None
        self.loop: asyncio.AbstractEventLoop | None = None
        self.timer: asyncio.TimerHandle | None = None

    def _store(self) -> NoticeHold:
        if self.store is None:
            self.store = NoticeHold()
        return self.store

    def _until(self):
        from graph.plugins.host import HOST

        return quiet_until(HOST.config() if HOST.config else None, time.time())

    def hold(self, notice) -> bool:
        if notice.importance == "high" or notice.kind == "question":
            return False
        try:
            with self.lock:
                if self._until() is None:
                    return False
                self._store().add(notice.text)
                if self.loop is not None:
                    try:
                        self.loop.call_soon_threadsafe(self.check)
                    except RuntimeError:
                        # Shutdown raced the producer; the durable row is recovered
                        # at next startup, so do not also dispatch it immediately.
                        log.warning("Quiet hours loop stopped; retained notice for startup")
                return True
        except Exception:
            log.exception("Could not hold notice; delivering normally")
            return False

    def start(self) -> None:
        self.loop = asyncio.get_running_loop()
        self.check()

    def stop(self) -> None:
        with self.lock:
            if self.timer is not None:
                self.timer.cancel()
                self.timer = None
            self.loop = None

    def check(self) -> None:
        """Recover on startup, or flush at window end without re-entering the hold."""
        from graph.plugins.chat_surface import ChatNotice, _dispatch_notice
        import uuid

        with self.lock:
            if self.loop is None:
                return
            if self.timer is not None:
                self.timer.cancel()
                self.timer = None
            delay = 60.0
            try:
                rows = self._store().rows()
                if not rows:
                    return
                until = self._until()
                if until is None:
                    now = time.time()
                    notice = ChatNotice(
                        kind="notify", importance="normal",
                        text=f"Quiet hours digest: {len(rows)} notices\n\n" + "\n".join(text for _, text in rows),
                        link="/app/", notice_id=uuid.uuid4().hex, created_at=now, expires_at=now + 3600,
                    )
                    # Expiry of the original notices is deliberately irrelevant.
                    if _dispatch_notice(notice):
                        self._store().clear_through(rows[-1][0])
                        return
                else:
                    delay = min(delay, max(0.0, until - time.time()))
            except Exception:
                log.exception("Could not flush quiet hours hold; will retry")
            # Re-read live settings while pending, including disable/invalid edits.
            self.timer = self.loop.call_later(delay, self.check)


DELIVERY = QuietHoursDelivery()
