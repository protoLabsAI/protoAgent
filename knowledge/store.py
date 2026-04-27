"""KnowledgeStore — sqlite-backed chunk storage with FTS5 search.

The template's default knowledge surface. One ``chunks`` table holds
every piece of stored content (operator notes via ``memory_ingest``,
daily-log entries, conversation findings extracted by
``MemoryMiddleware``); the ``domain`` column distinguishes them.

Search uses sqlite FTS5 when available (true on virtually all modern
sqlite builds). When FTS5 is missing — sandboxed sqlite, custom builds
— the store transparently falls back to ``LIKE`` keyword matching so
the API contract still holds.

The store is path-aware and degradation-aware:

- Honors ``KNOWLEDGE_DB_PATH`` env var → constructor argument →
  config default ``/sandbox/knowledge/agent.db``.
- If the configured path is unwritable (running locally outside the
  container, no /sandbox), falls back to ``~/.protoagent/knowledge/agent.db``
  so a fresh ``python server.py`` works without sudo.
- All write operations swallow ``sqlite3.DatabaseError`` (covers
  OperationalError, IntegrityError, and corruption variants) and log;
  the store never crashes the agent loop on a corrupt or read-only DB.

Forks that want embeddings on top of FTS5 can subclass and override
``search()`` — the middleware reads through that one method.
"""

from __future__ import annotations

import logging
import os
import re
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

DEFAULT_DB_PATH = "/sandbox/knowledge/agent.db"


@dataclass
class Chunk:
    """One row from the chunks table — what callers see."""
    id: int
    content: str
    domain: str
    heading: str | None
    source: str | None
    source_type: str | None
    finding_type: str | None
    created_at: str
    updated_at: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "content": self.content,
            "domain": self.domain,
            "heading": self.heading,
            "source": self.source,
            "source_type": self.source_type,
            "finding_type": self.finding_type,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }


def _resolve_path(db_path: str | Path | None) -> Path:
    """Pick a writable DB path. Env > arg > default; fall back to ~/.protoagent."""
    raw = os.environ.get("KNOWLEDGE_DB_PATH") or db_path or DEFAULT_DB_PATH
    p = Path(str(raw)).expanduser()
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        # Probe writability
        probe = p.parent / ".write-probe"
        probe.touch()
        probe.unlink()
        return p
    except OSError:
        fallback = Path.home() / ".protoagent" / "knowledge" / "agent.db"
        fallback.parent.mkdir(parents=True, exist_ok=True)
        log.info(
            "[knowledge] %s not writable; using %s instead",
            p, fallback,
        )
        return fallback


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


# LIKE escaping — sqlite treats ``%`` and ``_`` as wildcards in LIKE
# patterns. Without escaping, a search for ``"100%"`` matches every row
# starting with ``"100"`` instead of literal "100%". We escape them
# alongside the escape char itself, then bind ``ESCAPE '\'`` on every
# LIKE clause that takes user input.
_LIKE_ESCAPE = "\\"


def _escape_like(text: str) -> str:
    """Escape ``%``, ``_``, and the escape char for safe LIKE matching."""
    return (
        text
        .replace(_LIKE_ESCAPE, _LIKE_ESCAPE + _LIKE_ESCAPE)
        .replace("%", _LIKE_ESCAPE + "%")
        .replace("_", _LIKE_ESCAPE + "_")
    )


def _fts_quote(token: str) -> str:
    """Quote a token for FTS5 MATCH so it's treated as a literal phrase.

    FTS5 has its own query syntax (column filters, prefix wildcards,
    NEAR, AND/OR/NOT operators). Wrapping each token in double quotes
    forces FTS5 to interpret it as a phrase token, neutralising any
    operator characters the user happened to type. Internal double
    quotes are doubled per FTS5 phrase rules.
    """
    return '"' + token.replace('"', '""') + '"'


def _has_fts5(db: sqlite3.Connection) -> bool:
    try:
        db.execute(
            "CREATE VIRTUAL TABLE IF NOT EXISTS _fts5_probe USING fts5(x)"
        )
        db.execute("DROP TABLE _fts5_probe")
        return True
    except sqlite3.OperationalError:
        return False


_SCHEMA = """
CREATE TABLE IF NOT EXISTS chunks (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    content       TEXT NOT NULL,
    domain        TEXT NOT NULL DEFAULT 'general',
    heading       TEXT,
    source        TEXT,
    source_type   TEXT,
    finding_type  TEXT,
    created_at    TEXT NOT NULL,
    updated_at    TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_chunks_domain     ON chunks(domain);
CREATE INDEX IF NOT EXISTS idx_chunks_created_at ON chunks(created_at);
"""

_FTS_SCHEMA = """
CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5(
    content, heading, content='chunks', content_rowid='id'
);

CREATE TRIGGER IF NOT EXISTS chunks_ai AFTER INSERT ON chunks BEGIN
    INSERT INTO chunks_fts(rowid, content, heading)
        VALUES (new.id, new.content, new.heading);
END;

CREATE TRIGGER IF NOT EXISTS chunks_ad AFTER DELETE ON chunks BEGIN
    INSERT INTO chunks_fts(chunks_fts, rowid, content, heading)
        VALUES('delete', old.id, old.content, old.heading);
END;

CREATE TRIGGER IF NOT EXISTS chunks_au AFTER UPDATE ON chunks BEGIN
    INSERT INTO chunks_fts(chunks_fts, rowid, content, heading)
        VALUES('delete', old.id, old.content, old.heading);
    INSERT INTO chunks_fts(rowid, content, heading)
        VALUES (new.id, new.content, new.heading);
END;
"""


class KnowledgeStore:
    """Default knowledge store. Sqlite + FTS5 (with LIKE fallback).

    Forks usually don't subclass this — extend ``add_chunk`` /
    ``search`` directly when you need new fields, or wrap it with
    your own embedding layer.
    """

    def __init__(self, db_path: str | Path | None = None):
        self.path = _resolve_path(db_path)
        self._fts_available: bool | None = None
        self._init_db()

    # ── connection / schema ─────────────────────────────────────────────────

    def _connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(str(self.path))
        db.row_factory = sqlite3.Row
        # WAL is best-effort — read-only sqlite files (e.g. immutable
        # mounts) reject the PRAGMA. The connection stays usable for
        # reads; only writes will fail later, and those go through
        # the per-method OperationalError guards.
        try:
            db.execute("PRAGMA journal_mode=WAL")
        except sqlite3.OperationalError as exc:
            log.debug("[knowledge] PRAGMA journal_mode=WAL skipped: %s", exc)
        return db

    def _init_db(self) -> None:
        try:
            db = self._connect()
            db.executescript(_SCHEMA)
            self._fts_available = _has_fts5(db)
            if self._fts_available:
                db.executescript(_FTS_SCHEMA)
                # Re-index any pre-existing rows. The CREATE TRIGGER
                # statements only fire on subsequent inserts, so a DB
                # populated before FTS was added would have an empty
                # virtual table without this rebuild.
                try:
                    db.execute(
                        "INSERT INTO chunks_fts(chunks_fts) VALUES('rebuild')"
                    )
                except sqlite3.DatabaseError as exc:
                    log.debug("[knowledge] FTS rebuild skipped: %s", exc)
            else:
                log.info(
                    "[knowledge] FTS5 unavailable — search will use LIKE fallback"
                )
            db.commit()
            db.close()
        except sqlite3.DatabaseError:
            log.exception("[knowledge] schema init failed at %s", self.path)

    # Convenience for middleware that wants the raw connection. Kept
    # private so the public API stays small.
    def _get_db(self) -> sqlite3.Connection | None:
        try:
            return self._connect()
        except sqlite3.DatabaseError:
            log.exception("[knowledge] connect failed")
            return None

    # ── writes ──────────────────────────────────────────────────────────────

    def add_chunk(
        self,
        content: str,
        domain: str = "general",
        heading: str | None = None,
        *,
        source: str | None = None,
        source_type: str | None = None,
        finding_type: str | None = None,
    ) -> int | None:
        """Insert a chunk. Returns the new row id, or None on failure."""
        if not content or not content.strip():
            return None
        db = self._get_db()
        if db is None:
            return None
        try:
            now = _now_iso()
            cur = db.execute(
                "INSERT INTO chunks "
                "(content, domain, heading, source, source_type, finding_type, "
                "created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (content, domain, heading, source, source_type, finding_type, now, now),
            )
            db.commit()
            return int(cur.lastrowid)
        except sqlite3.DatabaseError:
            log.exception("[knowledge] add_chunk failed")
            return None
        finally:
            db.close()

    def add_finding(
        self,
        content: str,
        source: str = "conversation",
        source_type: str = "chat",
        finding_type: str = "insight",
    ) -> int | None:
        """Compatibility shim for ``MemoryMiddleware.after_agent``.

        Stored under ``domain='finding'`` so memory_list / memory_recall
        can surface them alongside operator-set chunks.
        """
        return self.add_chunk(
            content,
            domain="finding",
            source=source,
            source_type=source_type,
            finding_type=finding_type,
        )

    # ── reads ───────────────────────────────────────────────────────────────

    def search(
        self,
        query: str,
        k: int = 5,
        *,
        domain: str | None = None,
    ) -> list[dict[str, Any]]:
        """Top-k chunks matching ``query``. Shape matches what the
        ``KnowledgeMiddleware`` consumes: each result has ``table``,
        ``preview``, plus the underlying chunk fields.

        Uses FTS5 when available, else a tokenized LIKE fallback. Returns
        an empty list on no matches or DB failure (never raises).
        """
        if not query or not query.strip():
            return []
        db = self._get_db()
        if db is None:
            return []
        try:
            rows = self._search_fts(db, query, k, domain) if self._fts_available \
                else self._search_like(db, query, k, domain)
        except sqlite3.DatabaseError as exc:
            log.warning("[knowledge] search failed: %s", exc)
            rows = []
        finally:
            db.close()

        results: list[dict[str, Any]] = []
        for r in rows:
            preview = (r["heading"] + ": " if r["heading"] else "") + r["content"]
            results.append({
                "table": "chunks",
                "preview": preview[:240],
                **dict(r),
            })
        return results

    def _search_fts(
        self,
        db: sqlite3.Connection,
        query: str,
        k: int,
        domain: str | None,
    ) -> list[sqlite3.Row]:
        # Sanitize to FTS5-safe tokens; OR them so a multi-word query
        # matches any of the keywords (closer to LIKE behaviour).
        # Each token is double-quoted so FTS5 treats it as a literal
        # phrase rather than parsing operators (column filters, prefix
        # wildcards, NEAR, etc.) — even though ``[\w']+`` already
        # filters most special chars, defence in depth is cheap.
        tokens = [t for t in re.findall(r"[\w']+", query) if t]
        if not tokens:
            return []
        match = " OR ".join(_fts_quote(t) for t in tokens)
        if domain:
            return db.execute(
                "SELECT c.* FROM chunks_fts f "
                "JOIN chunks c ON c.id = f.rowid "
                "WHERE chunks_fts MATCH ? AND c.domain = ? "
                "ORDER BY rank LIMIT ?",
                (match, domain, k),
            ).fetchall()
        return db.execute(
            "SELECT c.* FROM chunks_fts f "
            "JOIN chunks c ON c.id = f.rowid "
            "WHERE chunks_fts MATCH ? "
            "ORDER BY rank LIMIT ?",
            (match, k),
        ).fetchall()

    def _search_like(
        self,
        db: sqlite3.Connection,
        query: str,
        k: int,
        domain: str | None,
    ) -> list[sqlite3.Row]:
        tokens = [t for t in re.findall(r"[\w']+", query) if t]
        if not tokens:
            return []
        # Score = number of tokens matched (rough recall-style ranking).
        # User-supplied tokens are LIKE-escaped so a query containing
        # ``%`` or ``_`` doesn't silently match every row; ESCAPE is
        # bound on each clause.
        like_clauses = " + ".join(
            "CASE WHEN content LIKE ? ESCAPE ? OR heading LIKE ? ESCAPE ? "
            "THEN 1 ELSE 0 END"
            for _ in tokens
        )
        params: list[Any] = []
        for t in tokens:
            needle = f"%{_escape_like(t)}%"
            params.extend([needle, _LIKE_ESCAPE, needle, _LIKE_ESCAPE])
        sql = (
            f"SELECT *, ({like_clauses}) AS score FROM chunks "
            "WHERE score > 0"
        )
        if domain:
            sql += " AND domain = ?"
            params.append(domain)
        sql += " ORDER BY score DESC, id DESC LIMIT ?"
        params.append(k)
        return db.execute(sql, params).fetchall()

    def list_chunks(
        self,
        domain: str | None = None,
        limit: int = 50,
    ) -> list[Chunk]:
        """Most-recent-first chunk listing. Used by ``memory_list``."""
        db = self._get_db()
        if db is None:
            return []
        try:
            if domain:
                rows = db.execute(
                    "SELECT * FROM chunks WHERE domain = ? ORDER BY id DESC LIMIT ?",
                    (domain, limit),
                ).fetchall()
            else:
                rows = db.execute(
                    "SELECT * FROM chunks ORDER BY id DESC LIMIT ?",
                    (limit,),
                ).fetchall()
        except sqlite3.DatabaseError as exc:
            log.warning("[knowledge] list_chunks failed: %s", exc)
            rows = []
        finally:
            db.close()
        return [Chunk(**dict(r)) for r in rows]

    def stats(self) -> dict[str, int]:
        """Return per-domain chunk counts plus a ``total`` key."""
        db = self._get_db()
        if db is None:
            return {"total": 0}
        try:
            rows = db.execute(
                "SELECT domain, COUNT(*) AS n FROM chunks GROUP BY domain ORDER BY n DESC"
            ).fetchall()
            total = db.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
        except sqlite3.DatabaseError as exc:
            log.warning("[knowledge] stats failed: %s", exc)
            return {"total": 0}
        finally:
            db.close()
        out = {r["domain"]: r["n"] for r in rows}
        out["total"] = int(total)
        return out

    # ── verification helpers (used by evals/verify.py) ──────────────────────

    def find_chunk_containing(
        self,
        text: str,
        domain: str | None = None,
    ) -> Chunk | None:
        """Return the most-recent chunk whose content or heading contains ``text``.

        Used by the eval runner to assert side-effect outcomes after a
        memory-writing turn. Empty / whitespace-only ``text`` returns
        ``None`` rather than building a ``LIKE '%%'`` predicate that
        would match every row.
        """
        if not text or not text.strip():
            return None
        db = self._get_db()
        if db is None:
            return None
        try:
            needle = f"%{_escape_like(text)}%"
            sql = (
                "SELECT * FROM chunks "
                "WHERE (content LIKE ? ESCAPE ? OR heading LIKE ? ESCAPE ?)"
            )
            params: list[Any] = [needle, _LIKE_ESCAPE, needle, _LIKE_ESCAPE]
            if domain:
                sql += " AND domain = ?"
                params.append(domain)
            sql += " ORDER BY id DESC LIMIT 1"
            row = db.execute(sql, params).fetchone()
        except sqlite3.DatabaseError as exc:
            log.warning("[knowledge] find_chunk_containing failed: %s", exc)
            row = None
        finally:
            db.close()
        return Chunk(**dict(row)) if row else None

    def delete_by_content(self, contains: str) -> int:
        """Delete chunks whose content matches ``%contains%``. Returns count.

        Empty / whitespace-only ``contains`` is a no-op — the alternative
        is ``DELETE WHERE content LIKE '%%'`` which wipes every row.
        """
        if not contains or not contains.strip():
            return 0
        db = self._get_db()
        if db is None:
            return 0
        try:
            cur = db.execute(
                "DELETE FROM chunks WHERE content LIKE ? ESCAPE ?",
                (f"%{_escape_like(contains)}%", _LIKE_ESCAPE),
            )
            db.commit()
            return int(cur.rowcount)
        except sqlite3.DatabaseError as exc:
            log.warning("[knowledge] delete_by_content failed: %s", exc)
            return 0
        finally:
            db.close()

    def delete_by_heading(self, domain: str, heading: str) -> int:
        """Delete chunks matching (domain, heading). Returns count."""
        db = self._get_db()
        if db is None:
            return 0
        try:
            cur = db.execute(
                "DELETE FROM chunks WHERE domain = ? AND heading = ?",
                (domain, heading),
            )
            db.commit()
            return int(cur.rowcount)
        except sqlite3.DatabaseError as exc:
            log.warning("[knowledge] delete_by_heading failed: %s", exc)
            return 0
        finally:
            db.close()
