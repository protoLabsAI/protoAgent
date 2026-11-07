"""Core memory storage in the private agent.db (ADR 0125); no delivery readers yet."""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
import time
from collections.abc import Iterator
from contextlib import closing, contextmanager
from dataclasses import dataclass
from pathlib import Path

from knowledge.hybrid_store import EmbedFn, rank_vectors, rrf_scores
from knowledge.store import _WRITE_LOCK_RETRY_S, _fts_quote, _normalize_expires_at, _now_iso, _resolve_path

log = logging.getLogger(__name__)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS memories (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT NOT NULL CHECK(kind IN ('profile','standing','fact','decision')),
    subject TEXT,
    heading TEXT,
    content TEXT NOT NULL,
    delivery TEXT NOT NULL DEFAULT 'retrieved' CHECK(delivery IN ('always','retrieved','on_demand')),
    review_state TEXT NOT NULL CHECK(review_state IN ('pending','confirmed','rejected')),
    provenance TEXT NOT NULL CHECK(provenance IN ('operator','agent','extracted')),
    scope TEXT,
    legacy_domain TEXT,
    valid_from TEXT,
    superseded_by INTEGER REFERENCES memories(id),
    expires_at TEXT,
    source_session TEXT,
    thread_id TEXT,
    invalidated_at TEXT,
    invalidation_reason TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_memories_subject_kind ON memories(subject, kind);
CREATE INDEX IF NOT EXISTS idx_memories_delivery ON memories(delivery);
CREATE INDEX IF NOT EXISTS idx_memories_created_at ON memories(created_at);
CREATE INDEX IF NOT EXISTS idx_memories_source_session ON memories(source_session);
CREATE VIRTUAL TABLE IF NOT EXISTS memories_fts USING fts5(
    heading, content, content='memories', content_rowid='id'
);
CREATE TRIGGER IF NOT EXISTS memories_ai AFTER INSERT ON memories BEGIN
    INSERT INTO memories_fts(rowid, heading, content) VALUES (new.id, new.heading, new.content);
END;
CREATE TRIGGER IF NOT EXISTS memories_ad AFTER DELETE ON memories BEGIN
    INSERT INTO memories_fts(memories_fts, rowid, heading, content)
        VALUES ('delete', old.id, old.heading, old.content);
    DELETE FROM memory_vectors WHERE memory_id = old.id;
END;
CREATE TRIGGER IF NOT EXISTS memories_au AFTER UPDATE OF heading, content ON memories BEGIN
    INSERT INTO memories_fts(memories_fts, rowid, heading, content)
        VALUES ('delete', old.id, old.heading, old.content);
    INSERT INTO memories_fts(rowid, heading, content) VALUES (new.id, new.heading, new.content);
    DELETE FROM memory_vectors WHERE memory_id = old.id;
END;
CREATE TABLE IF NOT EXISTS memory_vectors (memory_id INTEGER PRIMARY KEY, vec TEXT NOT NULL);
"""


@dataclass
class MemoryEntry:
    """One durable memory, including audit metadata."""

    id: int
    kind: str
    subject: str | None
    heading: str | None
    content: str
    delivery: str
    review_state: str
    provenance: str
    scope: str | None
    legacy_domain: str | None
    valid_from: str | None
    superseded_by: int | None
    expires_at: str | None
    source_session: str | None
    thread_id: str | None
    invalidated_at: str | None
    invalidation_reason: str | None
    created_at: str
    updated_at: str


class MemoryStore:
    """Synchronous, thread-safe SQLite memory with optional hybrid retrieval.

    Each operation owns a connection; writes are atomic and retry lock acquisition
    like HistoryStore. Embedding failures degrade to keyword search.
    """

    def __init__(self, db_path: str | Path | None = None, *, embed_fn: EmbedFn | None = None):
        self.path = _resolve_path(db_path)
        self._embed_fn = embed_fn
        self._embed_lock = threading.Lock()
        with closing(self._connect()) as db:
            db.executescript(_SCHEMA)

    def _connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(str(self.path))
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA busy_timeout=5000")
        try:
            db.execute("PRAGMA journal_mode=WAL")
        except sqlite3.OperationalError:
            pass
        return db

    @contextmanager
    def _write(self) -> Iterator[sqlite3.Connection]:
        attempt = 0
        while True:
            db = self._connect()
            try:
                db.execute("BEGIN IMMEDIATE")
                break
            except sqlite3.OperationalError as exc:
                db.close()
                attempt += 1
                if "locked" not in str(exc) or attempt > len(_WRITE_LOCK_RETRY_S):
                    raise
                time.sleep(_WRITE_LOCK_RETRY_S[attempt - 1])
        with closing(db), db:
            yield db

    def _embed(self, text: str) -> list[float] | None:
        if self._embed_fn is None:
            return None
        try:
            with self._embed_lock:
                return [float(x) for x in self._embed_fn(text)]
        except Exception as exc:  # noqa: BLE001 — embeddings are optional
            log.warning("[memory] embedding failed: %s; FTS5 only", exc)
            return None

    def add(
        self,
        content: str,
        *,
        kind: str,
        provenance: str,
        subject: str | None = None,
        heading: str | None = None,
        delivery: str = "retrieved",
        review_state: str | None = None,
        scope: str | None = None,
        expires_at: str | None = None,
        source_session: str | None = None,
        thread_id: str | None = None,
        legacy_domain: str | None = None,
    ) -> int:
        """Persist a memory and its embedding atomically; operator rows default confirmed."""
        review_state = (
            review_state if review_state is not None else ("confirmed" if provenance == "operator" else "pending")
        )
        vec = self._embed((heading + "\n" if heading else "") + content)
        now = _now_iso()
        with self._write() as db:
            cur = db.execute(
                "INSERT INTO memories (kind, subject, heading, content, delivery, review_state, provenance, "
                "scope, expires_at, source_session, thread_id, legacy_domain, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    kind,
                    subject,
                    heading,
                    content,
                    delivery,
                    review_state,
                    provenance,
                    scope,
                    _normalize_expires_at(expires_at),
                    source_session,
                    thread_id,
                    legacy_domain,
                    now,
                    now,
                ),
            )
            id = int(cur.lastrowid)
            if vec is not None:
                db.execute("INSERT INTO memory_vectors VALUES (?, ?)", (id, json.dumps(vec)))
            return id

    def get(self, id: int) -> MemoryEntry | None:
        """Read a row by id, including its audit state."""
        with closing(self._connect()) as db:
            row = db.execute("SELECT * FROM memories WHERE id = ?", (id,)).fetchone()
            return MemoryEntry(**dict(row)) if row else None

    @staticmethod
    def _filters(kind=None, delivery=None, review_state=None, include_superseded=False, deliverable=False):
        # Pending is eligible, identically to chunks' _deliverable_clauses. There
        # is deliberately no domain or legacy_domain delivery predicate here.
        where = [
            "m.invalidated_at IS NULL",
            "m.review_state != 'rejected'",
            "(m.expires_at IS NULL OR m.expires_at > ?)",
        ]
        params = [_now_iso()]
        if not include_superseded or deliverable:
            where.append("m.superseded_by IS NULL")
        for column, value in (("kind", kind), ("delivery", delivery), ("review_state", review_state)):
            if value is not None:
                where.append(f"m.{column} = ?")
                params.append(value)
        return " AND ".join(where), params

    def search(
        self,
        query: str,
        k: int = 5,
        *,
        kind=None,
        delivery=None,
        review_state=None,
        include_superseded: bool = False,
        deliverable: bool = False,
    ) -> list[MemoryEntry]:
        """Fuse FTS5 and cosine rankings; apply identical eligibility to both halves.

        Audit search may include superseded rows, but deliverable=True always
        excludes them. Invalidated, expired and rejected rows never enter search.
        """
        if not query.strip() or k <= 0:
            return []
        vec = self._embed(query)
        where, params = self._filters(kind, delivery, review_state, include_superseded, deliverable)
        candidates = max(k, 20)
        with closing(self._connect()) as db:
            rows = db.execute(
                "SELECT m.* FROM memories_fts JOIN memories m ON m.id = memories_fts.rowid "
                f"WHERE memories_fts MATCH ? AND {where} ORDER BY memories_fts.rank, m.id LIMIT ?",
                [" OR ".join(_fts_quote(t) for t in query.split()), *params, candidates],
            ).fetchall()
            by_id = {r["id"]: MemoryEntry(**dict(r)) for r in rows}
            if vec is None:
                return list(by_id.values())[:k]
            vector_rows = db.execute(
                f"SELECT m.*, v.vec FROM memory_vectors v JOIN memories m ON m.id = v.memory_id WHERE {where}",
                params,
            ).fetchall()
            vector_ids = rank_vectors(vec, ((r["id"], r["vec"]) for r in vector_rows), candidates)
            scores = rrf_scores((by_id, vector_ids))
            for row in vector_rows:
                data = dict(row)
                data.pop("vec")
                by_id[row["id"]] = MemoryEntry(**data)
            return [by_id[id] for id in sorted(scores, key=scores.get, reverse=True)[:k]]

    def list(
        self, *, kind=None, delivery=None, review_state=None, limit: int = 100, include_superseded: bool = False
    ) -> list[MemoryEntry]:
        """List eligible memories newest first, optionally including superseded rows."""
        if limit <= 0:
            return []
        where, params = self._filters(kind, delivery, review_state, include_superseded)
        with closing(self._connect()) as db:
            return [
                MemoryEntry(**dict(row))
                for row in db.execute(
                    f"SELECT m.* FROM memories m WHERE {where} ORDER BY created_at DESC, id DESC LIMIT ?",
                    [*params, limit],
                )
            ]

    def always_on(self, max_chars: int = 6000) -> str:
        """Format eligible always-on memories newest first within a strict character cap."""
        pieces = []
        total = 0
        for entry in self.list(delivery="always"):
            piece = (f"[{entry.heading}] " if entry.heading else "") + entry.content
            size = len(piece) + bool(pieces)
            if total + size > max_chars:
                break
            pieces.append(piece)
            total += size
        return "\n".join(pieces)

    def delete(self, id: int) -> bool:
        """Hard-delete a row; triggers remove its FTS and vector entries atomically."""
        with self._write() as db:
            return db.execute("DELETE FROM memories WHERE id = ?", (id,)).rowcount > 0

    def stats(self) -> dict[str, int]:
        """Stored counts by kind, including audit rows, plus total."""
        with closing(self._connect()) as db:
            counts = {row[0]: row[1] for row in db.execute("SELECT kind, COUNT(*) FROM memories GROUP BY kind")}
        return {"profile": 0, "standing": 0, "fact": 0, "decision": 0, **counts, "total": sum(counts.values())}

    def count(self) -> int:
        """Number of stored memories, including audit rows."""
        return self.stats()["total"]
