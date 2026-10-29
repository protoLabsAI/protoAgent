"""Core conversation history in agent.db (ADR 0125), without embeddings."""

from __future__ import annotations

import sqlite3
from collections.abc import Sequence
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from knowledge.store import FORGET_PENDING_PREFIX, _history_forget_where, _now_iso, _resolve_path

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

    # ── console History view (ADR 0125 D6) ───────────────────────────────────────
    # The operator-facing rollup/stats/delete surface for the Memory inspector's
    # History tab. History is visible only here — never in the Knowledge browser,
    # which lists chunks only.

    def sessions(self) -> list[dict]:
        """Per-session rollup of VISIBLE history, newest activity first: one row per
        ``session_id`` with its surface, date span (``span_*`` plus the ``created_at``
        range) and per-kind counts. Rows hidden by an in-flight forget
        (``invalidated_at`` set) are excluded, like every other read here."""
        with closing(self._connect()) as db:
            rows = db.execute(
                "SELECT session_id, COUNT(*) AS total, "
                "SUM(kind = 'archive') AS archives, "
                "SUM(kind = 'summary') AS summaries, "
                "SUM(kind = 'report') AS reports, "
                "MAX(surface) AS surface, "  # a non-null surface if any row carries one
                "MIN(span_start) AS span_start, MAX(span_end) AS span_end, "
                "MIN(created_at) AS first_at, MAX(created_at) AS last_at "
                "FROM history WHERE invalidated_at IS NULL "
                "GROUP BY session_id ORDER BY last_at DESC, session_id"
            ).fetchall()
        return [
            {
                "session_id": r["session_id"],
                "surface": r["surface"],
                "span_start": r["span_start"],
                "span_end": r["span_end"],
                "first_at": r["first_at"],
                "last_at": r["last_at"],
                "total": int(r["total"]),
                "kinds": {
                    "archive": int(r["archives"] or 0),
                    "summary": int(r["summaries"] or 0),
                    "report": int(r["reports"] or 0),
                },
            }
            for r in rows
        ]

    def stats(self) -> dict[str, int]:
        """VISIBLE per-kind entry counts plus ``total`` and distinct ``sessions`` — the
        history half of the console's memory/history size split (ADR 0125), so the
        operator sees history's real size apart from the chunk store."""
        with closing(self._connect()) as db:
            by_kind = {
                r["kind"]: int(r["n"])
                for r in db.execute(
                    "SELECT kind, COUNT(*) AS n FROM history WHERE invalidated_at IS NULL GROUP BY kind"
                )
            }
            sessions = db.execute(
                "SELECT COUNT(DISTINCT session_id) FROM history WHERE invalidated_at IS NULL"
            ).fetchone()[0]
        out = {"archive": 0, "summary": 0, "report": 0, **by_kind}
        out["total"] = sum(by_kind.values())
        out["sessions"] = int(sessions)
        return out

    def delete_entry(self, id: int) -> bool:
        """Hard-delete one VISIBLE entry by id — the History tab's single-entry delete. An
        explicit operator act removes the row outright (ADR 0069 D9), any kind; it never
        reaches a row another forget has hidden (``invalidated_at IS NULL``). Returns
        whether a row was removed."""
        with closing(self._connect()) as db, db:
            return db.execute(
                "DELETE FROM history WHERE id = ? AND invalidated_at IS NULL", (id,)
            ).rowcount > 0

    def delete_session(self, session_id: str, thread_ids: Sequence[str] = ()) -> int:
        """Hard-delete a whole session's history — the History tab's session delete, with
        the SAME semantics as the chat-delete forget (#3957): exactly the rows
        :func:`_history_forget_where` selects for ``session_id`` / ``thread_ids``, so
        background-job reports (``kind='report'``) are kept — the mirror of
        :meth:`KnowledgeStore.delete_history_for_session`. Returns the count removed; 0
        when nothing is selected (an empty selection never widens into every row)."""
        where, params = _history_forget_where(session_id, thread_ids)
        if not where:
            return 0
        with closing(self._connect()) as db, db:
            return int(db.execute(f"DELETE FROM history WHERE ({where})", params).rowcount)

    def mark_forget_pending(self, marker: str, *, session_id="", thread_ids=(), **kwargs) -> int:
        """Hide this session's archives and summaries before retirement."""
        where, params = _history_forget_where(session_id, thread_ids)
        if not marker or not where:
            return 0
        with closing(self._connect()) as db, db:
            return db.execute(
                f"UPDATE history SET invalidated_at = ?, invalidation_reason = ? "
                f"WHERE ({where}) AND invalidated_at IS NULL",
                [_now_iso(), marker, *params],
            ).rowcount

    def count_forget_pending(self, marker: str) -> int:
        """Count rows held by a previous attempt, so retries hide nothing new."""
        with closing(self._connect()) as db:
            return db.execute("SELECT COUNT(*) FROM history WHERE invalidation_reason = ?", (marker,)).fetchone()[0]

    def restore_forget_pending(self, marker: str) -> int:
        """Restore rows after a failed retirement."""
        with closing(self._connect()) as db, db:
            return db.execute(
                "UPDATE history SET invalidated_at = NULL, invalidation_reason = NULL WHERE invalidation_reason = ?",
                (marker,),
            ).rowcount

    def delete_forget_pending(self, marker: str) -> int:
        """Delete only the rows hidden before retirement, preserving its new harvest."""
        with closing(self._connect()) as db, db:
            return db.execute("DELETE FROM history WHERE invalidation_reason = ?", (marker,)).rowcount

    def forget_pending_markers(self) -> dict[str, str]:
        """Find interrupted history forgets for the orphan sweep."""
        with closing(self._connect()) as db:
            return dict(db.execute(
                "SELECT invalidation_reason, MIN(invalidated_at) FROM history "
                "WHERE substr(invalidation_reason, 1, ?) = ? GROUP BY invalidation_reason",
                (len(FORGET_PENDING_PREFIX), FORGET_PENDING_PREFIX),
            ).fetchall())
