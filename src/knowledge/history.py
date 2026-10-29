"""Core conversation history in agent.db (ADR 0125), without embeddings."""

from __future__ import annotations

import sqlite3
from collections.abc import Sequence
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from knowledge.store import _now_iso, _resolve_path

HistoryKind = Literal["archive", "summary", "report"]

_SCHEMA = """
CREATE TABLE IF NOT EXISTS history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL,
    thread_id TEXT,
    surface TEXT,
    kind TEXT NOT NULL CHECK (kind IN ('archive', 'summary', 'report')),
    heading TEXT,
    content TEXT NOT NULL,
    span_start TEXT,
    span_end TEXT,
    created_at TEXT NOT NULL,
    namespace TEXT,
    invalidated_at TEXT,
    invalidation_reason TEXT
);
CREATE INDEX IF NOT EXISTS idx_history_session ON history(session_id);
CREATE INDEX IF NOT EXISTS idx_history_thread ON history(thread_id);
CREATE VIRTUAL TABLE IF NOT EXISTS history_fts USING fts5(
    heading, content, content='history', content_rowid='id'
);
CREATE TRIGGER IF NOT EXISTS history_ai AFTER INSERT ON history BEGIN
    INSERT INTO history_fts(rowid, heading, content) VALUES (new.id, new.heading, new.content);
END;
CREATE TRIGGER IF NOT EXISTS history_ad AFTER DELETE ON history BEGIN
    INSERT INTO history_fts(history_fts, rowid, heading, content)
        VALUES ('delete', old.id, old.heading, old.content);
END;
CREATE TRIGGER IF NOT EXISTS history_au AFTER UPDATE OF heading, content ON history BEGIN
    INSERT INTO history_fts(history_fts, rowid, heading, content)
        VALUES ('delete', old.id, old.heading, old.content);
    INSERT INTO history_fts(rowid, heading, content) VALUES (new.id, new.heading, new.content);
END;
"""


@dataclass
class HistoryEntry:
    """One complete archive, summary or report; never a chunk."""

    id: int
    session_id: str
    thread_id: str | None
    surface: str | None
    kind: HistoryKind
    heading: str | None
    content: str
    span_start: str | None
    span_end: str | None
    created_at: str
    namespace: str | None
    invalidated_at: str | None
    invalidation_reason: str | None


class HistoryStore:
    """FTS5 history sharing the private knowledge database and forget transaction.

    Connections are short-lived, like KnowledgeStore. Errors propagate so archive
    callers can distinguish a durable write from a failed one.
    """

    def __init__(self, db_path: str | Path | None = None):
        self.path = _resolve_path(db_path)
        with closing(self._connect()) as db:
            db.executescript(_SCHEMA)

    def _connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(str(self.path))
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA busy_timeout=5000")
        try:
            db.execute("PRAGMA journal_mode=WAL")
        except sqlite3.OperationalError:
            pass  # Like KnowledgeStore, allow reads on read-only mounts.
        return db

    def add(
        self,
        content: str,
        *,
        session_id: str,
        kind: HistoryKind,
        thread_id: str | None = None,
        surface: str | None = None,
        heading: str | None = None,
        span_start: str | None = None,
        span_end: str | None = None,
        created_at: str | None = None,
        namespace: str | None = None,
        invalidated_at: str | None = None,
        invalidation_reason: str | None = None,
    ) -> int:
        """Persist one complete entry and return its id; raise on failure."""
        with closing(self._connect()) as db, db:
            cur = db.execute(
                "INSERT INTO history (session_id, thread_id, surface, kind, heading, content, "
                "span_start, span_end, created_at, namespace, invalidated_at, invalidation_reason) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (session_id, thread_id, surface, kind, heading, content, span_start, span_end,
                 created_at or _now_iso(), namespace, invalidated_at, invalidation_reason),
            )
            return int(cur.lastrowid)

    def get(self, id: int) -> HistoryEntry | None:
        """Return a visible entry, or None if absent or invalidated."""
        with closing(self._connect()) as db:
            row = db.execute("SELECT * FROM history WHERE id = ? AND invalidated_at IS NULL", (id,)).fetchone()
            return HistoryEntry(**dict(row)) if row else None

    def list_for_session(self, session_id: str, kinds: Sequence[HistoryKind] | None = None) -> list[HistoryEntry]:
        """List visible entries chronologically, optionally restricted to kinds."""
        where = "session_id = ? AND invalidated_at IS NULL"
        params: list = [session_id]
        if kinds is not None:
            if not kinds:
                return []
            where += f" AND kind IN ({','.join('?' for _ in kinds)})"
            params.extend(kinds)
        with closing(self._connect()) as db:
            return [HistoryEntry(**dict(row)) for row in db.execute(
                f"SELECT * FROM history WHERE {where} ORDER BY created_at, id", params
            )]

    def search(self, query: str, limit: int = 10) -> list[HistoryEntry]:
        """Search heading/content using FTS5 syntax, returning visible, kind-labelled hits."""
        if not query.strip() or limit <= 0:
            return []
        with closing(self._connect()) as db:
            return [HistoryEntry(**dict(row)) for row in db.execute(
                "SELECT h.* FROM history_fts JOIN history h ON h.id = history_fts.rowid "
                "WHERE history_fts MATCH ? AND h.invalidated_at IS NULL "
                "ORDER BY history_fts.rank, h.id LIMIT ?", (query, limit)
            )]
