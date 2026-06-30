"""KnowledgeStore — sqlite-backed chunk storage with FTS5 search.

The template's default knowledge surface. One ``chunks`` table holds
every piece of stored content (operator notes via ``memory_ingest``,
daily-log entries, conversation findings extracted by
``conversation_harvest``); the ``domain`` column distinguishes them.

Search uses sqlite FTS5 when available (true on virtually all modern
sqlite builds). When FTS5 is missing — sandboxed sqlite, custom builds
— the store transparently falls back to ``LIKE`` keyword matching so
the API contract still holds.

The store is path-aware and degradation-aware:

- Honors ``KNOWLEDGE_DB_PATH`` env var → constructor argument →
  config default ``/sandbox/knowledge/agent.db``.
- If the configured path is unwritable (running locally outside the
  container, no /sandbox), falls back to ``~/.protoagent/knowledge/agent.db``
  so a fresh ``python -m server`` works without sudo.
- All write operations swallow ``sqlite3.DatabaseError`` (covers
  OperationalError, IntegrityError, and corruption variants) and log;
  the store never crashes the agent loop on a corrupt or read-only DB.

Forks that want embeddings on top of FTS5 can subclass and override
``search()`` — the middleware reads through that one method. A worked
reference lives in ``knowledge/hybrid_store.py`` (``HybridKnowledgeStore``):
pluggable ``embed_fn``, RRF fusion of FTS5 + vector rankings, and an
embedding circuit breaker that falls back to FTS5 on outage.
"""

from __future__ import annotations

import logging
import os
import re
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

DEFAULT_DB_PATH = "/sandbox/knowledge/agent.db"

# Bounded concurrency for per-chunk contextual enrichment on ingest — the calls
# are independent aux-LLM requests, so we fan them out, but cap the burst on the
# gateway. Tune up for a faster gateway / down to be gentler.
_ENRICH_MAX_WORKERS = 8

# Fallback reasoning-stripper used if graph.output_format isn't importable (the
# store must never hard-depend on the graph package). Mirrors its scratch_pad /
# think / orphan-open rules.
_FALLBACK_REASONING_RE = re.compile(
    r"<scratch_pad>[\s\S]*?</scratch_pad>|<think>[\s\S]*?</think>"
    r"|<scratch_pad>[\s\S]*$|<think>[\s\S]*$",
    re.IGNORECASE,
)


def _strip_stored_reasoning(content: str) -> str:
    """ADR 0021 storage guardrail: scrub leaked model reasoning before persist.

    Prefers the canonical ``graph.output_format.strip_reasoning`` (lazy import,
    no knowledge→graph load cycle); falls back to a local regex if unavailable.
    """
    try:
        from graph.output_format import strip_reasoning

        return strip_reasoning(content).strip()
    except Exception:  # noqa: BLE001 — never let stripping break a write
        return _FALLBACK_REASONING_RE.sub("", content).strip()


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
    namespace: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "content": self.content,
            "domain": self.domain,
            "heading": self.heading,
            "source": self.source,
            "source_type": self.source_type,
            "finding_type": self.finding_type,
            "namespace": self.namespace,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }


def _resolve_path(db_path: str | Path | None, *, scoped: bool = True) -> Path:
    """Pick the DB path. ``KNOWLEDGE_DB_PATH`` env (or the ``db_path`` arg) is used
    verbatim; otherwise the per-instance ``instance_root/knowledge/agent.db`` store.

    ``scoped=False`` (ADR 0041, tiered stores) skips the ``KNOWLEDGE_DB_PATH`` env
    override and the per-instance default — the ``db_path`` is used verbatim. The
    shared **commons** knowledge store is host-level + un-scoped, so every agent on
    the box reads one DB regardless of ``instance.id`` (mirrors
    ``_resolve_skills_db(shared=True)``).
    """
    if not scoped:
        p = Path(db_path or DEFAULT_DB_PATH)
        p.parent.mkdir(parents=True, exist_ok=True)
        return p

    # ``KNOWLEDGE_DB_PATH`` env is an explicit operator override (verbatim). The
    # ``db_path`` arg is an override too UNLESS it's the legacy ``/sandbox`` default
    # (the config-field default), which now maps to the per-instance store.
    env = os.environ.get("KNOWLEDGE_DB_PATH", "").strip()
    if env:
        p = Path(env).expanduser()
    elif db_path and not str(db_path).startswith("/sandbox"):
        p = Path(db_path).expanduser()
    else:
        from infra.paths import instance_paths

        p = instance_paths().store("knowledge") / "agent.db"
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


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
        text.replace(_LIKE_ESCAPE, _LIKE_ESCAPE + _LIKE_ESCAPE)
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
        db.execute("CREATE VIRTUAL TABLE IF NOT EXISTS _fts5_probe USING fts5(x)")
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
    namespace     TEXT,
    created_at    TEXT NOT NULL,
    updated_at    TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_chunks_domain     ON chunks(domain);
CREATE INDEX IF NOT EXISTS idx_chunks_created_at ON chunks(created_at);

CREATE TABLE IF NOT EXISTS _kb_meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);
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

    def __init__(
        self,
        db_path: str | Path | None = None,
        *,
        preview_chars: int = 1000,
        chunk_max_chars: int = 1200,
        chunk_overlap_chars: int = 150,
        chunk_min_chars: int = 200,
        context_fn: Callable[[str, str], str] | None = None,
        scoped: bool = True,
    ):
        # scoped=False → the host-level shared commons (un-scoped path, ADR 0041).
        self.path = _resolve_path(db_path, scoped=scoped)
        self._fts_available: bool | None = None
        # How much of each hit's `heading: content` is returned as the `preview`
        # the model sees on recall. Bumped from a hardcoded 240 (RAG bake-off:
        # bigger previews carry more answer-bearing context at no retrieval cost).
        self._preview_chars = max(1, int(preview_chars))
        # Document chunking defaults for ``add_document`` (ADR 0021) — a large
        # ingest (conversation summary, pasted doc) is split into coherent,
        # overlapping pieces so each gets its own embedding instead of one
        # diluted vector. Per-call args override these.
        self._chunk_max_chars = max(1, int(chunk_max_chars))
        self._chunk_overlap_chars = max(0, int(chunk_overlap_chars))
        self._chunk_min_chars = max(0, int(chunk_min_chars))
        # Contextual Retrieval (ADR 0021): an injected ``(doc, chunk) -> context``
        # callable. When set, ``add_document`` prepends a one-line context to each
        # chunk of a multi-chunk doc before storing, so the chunk's embedding + FTS
        # terms carry document-level context. None = off (default). The store stays
        # LLM-agnostic — it just calls the injected fn.
        self._context_fn = context_fn
        self._init_db()

    # ── connection / schema ─────────────────────────────────────────────────

    def _connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(str(self.path))
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA busy_timeout=5000")  # wait (don't error) on lock contention
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
            # Migration: add the namespace column to DBs created before it
            # existed (ADR 0021). Additive + nullable, so it's a filter dimension
            # later (per-project scoping, ADR 0007) — never a migration then.
            try:
                cols = {r[1] for r in db.execute("PRAGMA table_info(chunks)")}
                if "namespace" not in cols:
                    db.execute("ALTER TABLE chunks ADD COLUMN namespace TEXT")
                # Index created after the column exists (new + migrated DBs alike).
                db.execute("CREATE INDEX IF NOT EXISTS idx_chunks_namespace ON chunks(namespace)")
            except sqlite3.DatabaseError as exc:
                log.debug("[knowledge] namespace migration skipped: %s", exc)
            self._fts_available = _has_fts5(db)
            if self._fts_available:
                db.executescript(_FTS_SCHEMA)
                # Re-index any pre-existing rows. The CREATE TRIGGER
                # statements only fire on subsequent inserts, so a DB
                # populated before FTS was added would have an empty
                # virtual table without this rebuild.
                try:
                    db.execute("INSERT INTO chunks_fts(chunks_fts) VALUES('rebuild')")
                except sqlite3.DatabaseError as exc:
                    log.debug("[knowledge] FTS rebuild skipped: %s", exc)
            else:
                log.info("[knowledge] FTS5 unavailable — search will use LIKE fallback")
            db.commit()
            db.close()
        except sqlite3.DatabaseError:
            log.exception("[knowledge] schema init failed at %s", self.path)

    # ── store metadata (key/value) ──────────────────────────────────────────
    # Used to STAMP the shared commons with the embed model it was built on
    # (ADR 0041 / bd-2wu): a fleet sharing a commons must share one embed model,
    # or its vectors are incompatible. The guard lives in the store builder.

    def get_meta(self, key: str) -> str | None:
        db = self._get_db()
        if db is None:
            return None
        try:
            row = db.execute("SELECT value FROM _kb_meta WHERE key = ?", (key,)).fetchone()
            return row["value"] if row else None
        except sqlite3.DatabaseError:
            return None
        finally:
            db.close()

    def set_meta(self, key: str, value: str) -> None:
        db = self._get_db()
        if db is None:
            return
        try:
            db.execute(
                "INSERT INTO _kb_meta(key, value) VALUES(?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, value),
            )
            db.commit()
        except sqlite3.DatabaseError as exc:
            log.warning("[knowledge] set_meta(%s) failed: %s", key, exc)
        finally:
            db.close()

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
        namespace: str | None = None,
    ) -> int | None:
        """Insert a chunk. Returns the new row id, or None on failure.

        Every write funnels through here, so this is where the ADR 0021
        guardrail lives: the model's internal reasoning must never reach the
        store. We strip ``<scratch_pad>``/``<think>`` defensively — covering all
        writers (memory tools, ingest, harvest, future ones), not just the ones
        that remember to clean their input.
        """
        if not content or not content.strip():
            return None
        content = _strip_stored_reasoning(content)
        if not content.strip():
            return None
        db = self._get_db()
        if db is None:
            return None
        try:
            now = _now_iso()
            cur = db.execute(
                "INSERT INTO chunks "
                "(content, domain, heading, source, source_type, finding_type, "
                "namespace, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (content, domain, heading, source, source_type, finding_type, namespace, now, now),
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
        *,
        namespace: str | None = None,
    ) -> int | None:
        """Add a finding chunk (``domain='finding'``) so memory_list /
        memory_recall surface it alongside operator-set chunks. ``namespace``
        carries an optional per-project/owner scope (ADR 0021)."""
        return self.add_chunk(
            content,
            domain="finding",
            source=source,
            source_type=source_type,
            finding_type=finding_type,
            namespace=namespace,
        )

    def add_document(
        self,
        content: str,
        domain: str = "general",
        heading: str | None = None,
        *,
        source: str | None = None,
        source_type: str | None = None,
        finding_type: str | None = None,
        namespace: str | None = None,
        max_chars: int | None = None,
        overlap_chars: int | None = None,
        min_chars: int | None = None,
        enrich: bool | None = None,
    ) -> list[int]:
        """Chunk a document, then store each piece via ``add_chunk``.

        For genuinely document-sized ingest (conversation summaries, pasted
        docs) — splitting into coherent, overlapping pieces means each gets its
        own embedding rather than one diluted vector for the whole thing, so
        semantic recall can land on the passage that actually answers a query
        (ADR 0021). Each piece funnels through ``add_chunk``, so the
        reasoning-strip guard (and, on a hybrid store, per-piece embedding) all
        still apply.

        When a ``context_fn`` is configured (Contextual Retrieval) and the doc
        actually splits, a one-line context situating each piece in the whole
        document is prepended before storage — so the piece's embedding and FTS
        terms carry document-level context. ``enrich=False`` forces it off for a
        call; the default follows whether a ``context_fn`` is set.

        Content at or under the chunk size is a single ``add_chunk`` — so it's a
        safe drop-in for ``add_chunk`` on any path that might receive a large
        body. Returns the created row ids (a failed piece is skipped, never
        aborts the rest)."""
        texts = self._chunk_and_enrich(
            content,
            max_chars=max_chars,
            overlap_chars=overlap_chars,
            min_chars=min_chars,
            enrich=enrich,
        )
        ids: list[int] = []
        for text in texts:
            cid = self.add_chunk(
                text,
                domain=domain,
                heading=heading,
                source=source,
                source_type=source_type,
                finding_type=finding_type,
                namespace=namespace,
            )
            if cid is not None:
                ids.append(cid)
        return ids

    def _chunk_and_enrich(
        self,
        content: str,
        *,
        max_chars: int | None = None,
        overlap_chars: int | None = None,
        min_chars: int | None = None,
        enrich: bool | None = None,
    ) -> list[str]:
        """Split a document into chunks and prepend per-chunk Contextual
        Retrieval context (when a ``context_fn`` is set and the doc actually
        splits). Returns the final texts to store/embed — shared by the base
        ``add_document`` and the hybrid store's batched-embedding override."""
        from knowledge.chunking import chunk_text

        pieces = chunk_text(
            content,
            max_chars=self._chunk_max_chars if max_chars is None else max_chars,
            overlap_chars=self._chunk_overlap_chars if overlap_chars is None else overlap_chars,
            min_chars=self._chunk_min_chars if min_chars is None else min_chars,
        )
        # Enrich only a genuinely multi-chunk doc: a single chunk IS the whole
        # document, so there's no within-document context to add (and no extra
        # LLM call for the common small-body case).
        enriching = self._context_fn is not None and (enrich if enrich is not None else True) and len(pieces) >= 2
        if not enriching:
            return list(pieces)
        contexts = self._enrich_contexts(content, pieces)
        return [f"{ctx}\n\n{piece}" if ctx else piece for piece, ctx in zip(pieces, contexts)]

    def _enrich_contexts(self, content: str, pieces: list[str]) -> list[str]:
        """Generate the per-chunk Contextual Retrieval context strings.

        Probe with the FIRST chunk serially: a failure there means the gateway
        is down, so disable enrichment for the whole doc (raw chunks) rather than
        firing N concurrent failing calls. If the probe succeeds, the remaining
        chunks are enriched CONCURRENTLY (independent calls; bounded pool) — these
        aux-LLM calls dominate ingest latency when run serially. A per-chunk
        failure in the parallel batch degrades just that chunk to raw."""
        try:
            first = (self._context_fn(content, pieces[0]) or "").strip()
        except Exception as exc:  # noqa: BLE001 — gateway down → no enrichment for the doc
            log.warning("[knowledge] context enrichment failed: %s; raw chunks", exc)
            return [""] * len(pieces)

        rest = pieces[1:]
        if not rest:
            return [first]

        def _one(piece: str) -> str:
            try:
                return (self._context_fn(content, piece) or "").strip()
            except Exception as exc:  # noqa: BLE001 — degrade just this chunk to raw
                log.warning("[knowledge] context enrichment failed for a chunk: %s", exc)
                return ""

        from concurrent.futures import ThreadPoolExecutor

        with ThreadPoolExecutor(max_workers=min(_ENRICH_MAX_WORKERS, len(rest))) as pool:
            rest_contexts = list(pool.map(_one, rest))  # order preserved
        return [first, *rest_contexts]

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
            rows = (
                self._search_fts(db, query, k, domain)
                if self._fts_available
                else self._search_like(db, query, k, domain)
            )
        except sqlite3.DatabaseError as exc:
            log.warning("[knowledge] search failed: %s", exc)
            rows = []
        finally:
            db.close()

        results: list[dict[str, Any]] = []
        for r in rows:
            preview = (r["heading"] + ": " if r["heading"] else "") + r["content"]
            results.append(
                {
                    "table": "chunks",
                    "preview": preview[: self._preview_chars],
                    **dict(r),
                }
            )
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
            "CASE WHEN content LIKE ? ESCAPE ? OR heading LIKE ? ESCAPE ? THEN 1 ELSE 0 END" for _ in tokens
        )
        params: list[Any] = []
        for t in tokens:
            needle = f"%{_escape_like(t)}%"
            params.extend([needle, _LIKE_ESCAPE, needle, _LIKE_ESCAPE])
        sql = f"SELECT *, ({like_clauses}) AS score FROM chunks WHERE score > 0"
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
        *,
        namespace: str | None = None,
    ) -> list[Chunk]:
        """Most-recent-first chunk listing. Used by ``memory_list`` and the
        fact consolidator. ``namespace`` (ADR 0021) optionally scopes to one
        per-project/owner bucket."""
        db = self._get_db()
        if db is None:
            return []
        clauses: list[str] = []
        params: list[Any] = []
        if domain:
            clauses.append("domain = ?")
            params.append(domain)
        if namespace is not None:
            clauses.append("namespace = ?")
            params.append(namespace)
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        params.append(limit)
        try:
            rows = db.execute(f"SELECT * FROM chunks{where} ORDER BY id DESC LIMIT ?", params).fetchall()
        except sqlite3.DatabaseError as exc:
            log.warning("[knowledge] list_chunks failed: %s", exc)
            rows = []
        finally:
            db.close()
        return [Chunk(**dict(r)) for r in rows]

    def delete_by_id(self, chunk_id: int) -> bool:
        """Delete one chunk by id. Used by the fact consolidator to replace a
        superseded fact (ADR 0021). Returns True if a row was removed."""
        db = self._get_db()
        if db is None:
            return False
        try:
            cur = db.execute("DELETE FROM chunks WHERE id = ?", (chunk_id,))
            db.commit()
            return cur.rowcount > 0
        except sqlite3.DatabaseError:
            log.exception("[knowledge] delete_by_id failed")
            return False
        finally:
            db.close()

    def get_hot_memory(self, max_chars: int = 6000) -> str:
        """Concatenate every ``domain="hot"`` chunk for always-on injection.

        "Hot" chunks are operator facts that should be in front of the model
        every turn (vs. retrieved-on-relevance). ``KnowledgeMiddleware`` reads
        this each turn so a newly-added hot fact is seen immediately. Returns
        "" when there are none; trims oldest-first if over ``max_chars``.
        """
        chunks = self.list_chunks(domain="hot", limit=100)  # newest-first
        formatted: list[str] = []
        total = 0
        for c in chunks:  # newest-first → oldest trimmed when over budget
            piece = (f"[{c.heading}] " if c.heading else "") + c.content
            if total + len(piece) > max_chars:
                break
            formatted.append(piece)
            total += len(piece)
        return "\n".join(formatted)

    def stats(self) -> dict[str, int]:
        """Return per-domain chunk counts plus a ``total`` key."""
        db = self._get_db()
        if db is None:
            return {"total": 0}
        try:
            rows = db.execute("SELECT domain, COUNT(*) AS n FROM chunks GROUP BY domain ORDER BY n DESC").fetchall()
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
            sql = "SELECT * FROM chunks WHERE (content LIKE ? ESCAPE ? OR heading LIKE ? ESCAPE ?)"
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

    def get_chunk(self, chunk_id: int) -> dict | None:
        """Return one chunk's full row as a dict by id, or None. The reader the
        layered store's ``promote`` uses to copy a private chunk into the commons."""
        db = self._get_db()
        if db is None:
            return None
        try:
            row = db.execute("SELECT * FROM chunks WHERE id = ?", (int(chunk_id),)).fetchone()
            return dict(row) if row else None
        except sqlite3.DatabaseError:
            return None
        finally:
            db.close()

    def id_for_exact_content(self, content: str) -> int | None:
        """Id of a chunk whose content is EXACTLY ``content`` (not a LIKE), else None.
        Keeps promotion idempotent — a chunk already in the commons isn't duplicated."""
        if not content:
            return None
        db = self._get_db()
        if db is None:
            return None
        try:
            row = db.execute(
                "SELECT id FROM chunks WHERE content = ? ORDER BY id LIMIT 1", (content,)
            ).fetchone()
            return int(row["id"]) if row else None
        except sqlite3.DatabaseError:
            return None
        finally:
            db.close()

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

    def delete_by_namespace(self, namespace: str) -> int:
        """Delete every chunk in ``namespace``. Used to clean up ephemeral,
        session-scoped chunks (e.g. chat attachments) when the session is
        retired (ADR 0021). Returns the count removed."""
        if not namespace:
            return 0
        db = self._get_db()
        if db is None:
            return 0
        try:
            cur = db.execute("DELETE FROM chunks WHERE namespace = ?", (namespace,))
            db.commit()
            return int(cur.rowcount)
        except sqlite3.DatabaseError as exc:
            log.warning("[knowledge] delete_by_namespace failed: %s", exc)
            return 0
        finally:
            db.close()
