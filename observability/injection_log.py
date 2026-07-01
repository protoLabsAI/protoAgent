"""Per-turn memory-injection record (ADR 0069 D6).

One append-only row per model call that had memory auto-injected: which
prior-session digest entries, hot-memory chunks, and RAG hits entered the
prompt, when, for which session, at what approximate token cost. This is the
forensics substrate for "why did it say that?" and for detecting
SpAIware-class memory poisoning — the store row → source session → turns it
was injected into chain ends here.

Written best-effort from ``KnowledgeMiddleware.before_model`` (a write failure
never breaks a turn); read by the operator console via
``GET /api/memory/injections`` (``operator_api/injection_routes.py``).
Instance-scoped SQLite at ``instance_root/memory-injections.db`` (ADR 0004),
following the ``TelemetryStore`` conventions (WAL, busy_timeout, connection
per call).
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

log = logging.getLogger(__name__)

# The id-list columns, stored as JSON arrays (TEXT) and decoded on read.
_JSON_COLUMNS = ("digest_session_ids", "hot_chunk_ids", "rag_chunk_ids")


class InjectionLog:
    def __init__(self, db_path: str) -> None:
        self.path = str(db_path)
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
                CREATE TABLE IF NOT EXISTS injections (
                    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
                    ts                 TEXT NOT NULL,
                    session_id         TEXT NOT NULL DEFAULT '',
                    digest_session_ids TEXT NOT NULL DEFAULT '[]',
                    hot_chunk_ids      TEXT NOT NULL DEFAULT '[]',
                    rag_chunk_ids      TEXT NOT NULL DEFAULT '[]',
                    approx_tokens      INTEGER NOT NULL DEFAULT 0
                )
                """
            )
            db.execute("CREATE INDEX IF NOT EXISTS ix_injections_session ON injections(session_id)")
            db.commit()
        finally:
            db.close()

    def record(
        self,
        *,
        session_id: str = "",
        digest_session_ids: list[str] | None = None,
        hot_chunk_ids: list[int] | None = None,
        rag_chunk_ids: list[int] | None = None,
        approx_tokens: int = 0,
    ) -> None:
        """Append one injection row. Best-effort — never raises (a telemetry
        write must not break the model call that triggered it)."""
        try:
            db = self._connect()
        except sqlite3.DatabaseError:
            log.warning("[injection-log] connect failed at %s", self.path)
            return
        try:
            db.execute(
                "INSERT INTO injections "
                "(ts, session_id, digest_session_ids, hot_chunk_ids, rag_chunk_ids, approx_tokens) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (
                    datetime.now(UTC).isoformat(),
                    session_id or "",
                    json.dumps(list(digest_session_ids or [])),
                    json.dumps(list(hot_chunk_ids or [])),
                    json.dumps(list(rag_chunk_ids or [])),
                    int(approx_tokens),
                ),
            )
            db.commit()
        except sqlite3.DatabaseError as exc:
            log.warning("[injection-log] record failed: %s", exc)
        finally:
            db.close()

    def recent(self, session_id: str | None = None, limit: int = 50) -> list[dict]:
        """Injection rows, newest first, id-list columns decoded to Python lists.

        ``session_id`` empty/None → all sessions (the empty string is a filter
        VALUE only for rows that recorded no session identity, so it is treated
        as "no filter" — matching the route's querystring semantics)."""
        where, params = "", []
        if session_id:
            where, params = "WHERE session_id = ?", [session_id]
        db = self._connect()
        try:
            rows = db.execute(
                f"SELECT * FROM injections {where} ORDER BY id DESC LIMIT ?",
                [*params, max(1, int(limit))],
            ).fetchall()
        except sqlite3.DatabaseError as exc:
            log.warning("[injection-log] recent failed: %s", exc)
            return []
        finally:
            db.close()
        out: list[dict] = []
        for r in rows:
            d = dict(r)
            for col in _JSON_COLUMNS:
                try:
                    d[col] = json.loads(d.get(col) or "[]")
                except (json.JSONDecodeError, TypeError):
                    d[col] = []
            out.append(d)
        return out


# ---------------------------------------------------------------------------
# Default per-instance log (lazy singleton)
# ---------------------------------------------------------------------------

_default_log: InjectionLog | None = None


def injection_log() -> InjectionLog:
    """The per-instance injection log, created lazily on first use — NOT at
    import time (env identity is finalized after this module imports; mirrors
    ``memory_path()``). Shared by the middleware writer and the operator read
    route so both see one DB. ``PROTOAGENT_INJECTION_LOG`` env overrides the
    path verbatim (same convention as ``MEMORY_PATH``; the test suite points
    it at a temp DB so runs never append to a real instance store)."""
    global _default_log
    if _default_log is None:
        raw = os.environ.get("PROTOAGENT_INJECTION_LOG", "").strip()
        if raw:
            _default_log = InjectionLog(str(Path(raw).expanduser()))
        else:
            from infra.paths import instance_paths

            _default_log = InjectionLog(str(instance_paths().store("memory-injections.db")))
    return _default_log


def reset_injection_log() -> None:
    """Drop the lazy singleton so the next ``injection_log()`` re-resolves its
    path from the environment (mirrors ``infra.paths.reset_instance_paths``)."""
    global _default_log
    _default_log = None
