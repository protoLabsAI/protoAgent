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
from datetime import UTC, datetime
from pathlib import Path

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
    task_id     TEXT
);
CREATE INDEX IF NOT EXISTS idx_activity_created_at ON activity(created_at);
"""


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


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
            db.commit()
            db.close()
        except sqlite3.DatabaseError:
            log.exception("[activity] schema init failed at %s", self.path)

    def _connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.path)
        db.execute("PRAGMA journal_mode=WAL")   # concurrent reads during writes
        db.execute("PRAGMA busy_timeout=5000")  # wait (don't error) on lock contention
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
    ) -> int | None:
        """Append a feed entry. ``origin`` empty ⇒ "operator" (a reply/live turn)."""
        if not text or not text.strip():
            return None
        try:
            db = self._connect()
            cur = db.execute(
                "INSERT INTO activity "
                "(created_at, context_id, origin, trigger, priority, state, text, task_id) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (_now_iso(), context_id, origin or "operator", trigger, priority, state, text, task_id),
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

    def recent(self, limit: int = 50) -> list[dict]:
        """Most-recent-first feed entries."""
        try:
            db = self._connect()
            rows = db.execute(
                "SELECT id, created_at, context_id, origin, trigger, priority, state, text, task_id "
                "FROM activity ORDER BY id DESC LIMIT ?",
                (limit,),
            ).fetchall()
            db.close()
            return [dict(r) for r in rows]
        except sqlite3.DatabaseError:
            log.warning("[activity] recent failed")
            return []
