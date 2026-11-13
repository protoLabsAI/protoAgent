"""ActivityLog — the provenance feed behind the Activity surface (ADR 0022).

One row per terminal turn in the Activity context: *what the agent produced* +
*what triggered it* (origin / trigger label / inbox priority) + when. This is the
timeline source for the console feed — a distinct concern from telemetry
(cost/latency) and from the checkpointer (the continuable conversation), so it
gets its own small store. The checkpointer still holds the thread for
continuation; this holds the legible "why".
"""

from __future__ import annotations

import logging
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

from observability.sqlite_migrate import add_missing_columns

log = logging.getLogger(__name__)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS activity (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at  TEXT NOT NULL,
    context_id  TEXT NOT NULL,
    origin      TEXT NOT NULL DEFAULT '',
    trigger     TEXT,
    priority    TEXT,
    state       TEXT,
    text        TEXT NOT NULL,
    task_id     TEXT,
    stimulus    TEXT,
    origin_session TEXT,
    inbox_id    INTEGER
);
CREATE INDEX IF NOT EXISTS idx_activity_created_at ON activity(created_at);
"""


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _coerce_inbox_id(value: object) -> int | None:
    """``inbox_id`` arrives from turn metadata as an int or a numeric string;
    anything else (None, "", non-numeric) stores as NULL rather than raising."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        try:
            return int(value.strip())
        except ValueError:
            return None
    return None


class ActivityLog:
    """SQLite-backed provenance feed. Best-effort: never raises into the loop."""

    def __init__(self, db_path: str) -> None:
        self.path = str(db_path)
        try:
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        except OSError:
            pass
        try:
            db = self._connect()
            db.executescript(_SCHEMA)
            # Additive migrations: `stimulus` (the triggering input text, so a response can
            # be shown as "in response to X"), `origin_session` (ADR 0129 — the chat a
            # job-session turn's schedule was created from, when there is one) and
            # `inbox_id` (docs/design/feed.md §3 — the inbox item the turn came from, so
            # the Feed can join a queued item to its result) were added after the table
            # shipped. Only ALTERed when missing, and a failed ALTER raises instead of
            # passing as done.
            add_missing_columns(
                db, "activity",
                (("stimulus", "TEXT"), ("origin_session", "TEXT"), ("inbox_id", "INTEGER")),
            )
            db.commit()
            db.close()
        except sqlite3.DatabaseError:
            log.exception("[activity] schema init failed at %s", self.path)

    def _connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.path)
        # busy_timeout FIRST: the WAL pragma itself takes a lock, so setting the
        # timeout after it leaves an unguarded window that raises "database is
        # locked" under contention (#2428 — wide enough to flake on Windows).
        db.execute("PRAGMA busy_timeout=5000")  # wait (don't error) on lock contention
        db.execute("PRAGMA journal_mode=WAL")  # concurrent reads during writes
        db.row_factory = sqlite3.Row
        return db

    def add(
        self,
        *,
        context_id: str,
        origin: str,
        text: str,
        trigger: str = "",
        priority: str = "",
        state: str = "completed",
        task_id: str = "",
        stimulus: str = "",
        origin_session: str = "",
        inbox_id: int | None = None,
    ) -> int | None:
        """Append a feed entry. ``origin`` empty ⇒ "operator" (a reply/live turn).
        ``stimulus`` is the triggering input text (the scheduled prompt / inbound message),
        so the feed can show the response as an explicit reply to it. ``origin_session``
        (ADR 0129) is the chat a job-session turn's work was created from, when known —
        ``context_id`` stays the turn's real execution context. ``inbox_id``
        (docs/design/feed.md §3) is the inbox item the turn ran, when known."""
        if not text or not text.strip():
            return None
        try:
            db = self._connect()
            cur = db.execute(
                "INSERT INTO activity "
                "(created_at, context_id, origin, trigger, priority, state, text, task_id, stimulus, "
                "origin_session, inbox_id) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    _now_iso(), context_id, origin or "operator", trigger, priority, state,
                    text, task_id, stimulus, origin_session, _coerce_inbox_id(inbox_id),
                ),
            )
            db.commit()
            return int(cur.lastrowid)
        except sqlite3.DatabaseError:
            log.exception("[activity] add failed")
            return None
        finally:
            try:
                db.close()
            except Exception:  # noqa: BLE001
                pass

    def prune(self, keep_days: int = 90, *, now: datetime | None = None) -> int:
        """Delete entries older than ``keep_days``. 0 = keep forever. Returns rows removed."""
        if keep_days == 0:
            return 0
        now = now or datetime.now(UTC)
        cutoff = (now - timedelta(days=keep_days)).isoformat()
        try:
            db = self._connect()
            cur = db.execute("DELETE FROM activity WHERE created_at < ?", (cutoff,))
            db.commit()
            return cur.rowcount
        except sqlite3.DatabaseError:
            log.exception("[activity] prune failed")
            return 0
        finally:
            try:
                db.close()
            except Exception:  # noqa: BLE001
                pass

    def recent(self, limit: int = 50) -> list[dict]:
        """Most-recent-first feed entries."""
        try:
            db = self._connect()
            rows = db.execute(
                "SELECT id, created_at, context_id, origin, trigger, priority, state, text, task_id, stimulus, "
                "origin_session, inbox_id FROM activity ORDER BY id DESC LIMIT ?",
                (limit,),
            ).fetchall()
            db.close()
            return [dict(r) for r in rows]
        except sqlite3.DatabaseError:
            log.warning("[activity] recent failed")
            return []
