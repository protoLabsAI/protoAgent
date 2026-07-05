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
from datetime import UTC, datetime, timedelta
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
    invalidated_at: str | None = None
    epoch: str | None = None
    # Why the row was invalidated: NULL = auto-supersession audit history (ADR 0069
    # D9, kept forever); ``_BULK_DELETE_REASON`` = a reversible bulk delete-by-source
    # (#1770) that the grace sweep may eventually reap.
    invalidation_reason: str | None = None

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
            "epoch": self.epoch,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "invalidated_at": self.invalidated_at,
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


def _normalize_before(before: str | datetime | None) -> str | None:
    """Normalize a purge cutoff to the stored ``created_at`` format (UTC ISO-8601)
    so string comparison in SQL is well-defined. ``None`` passes through (no
    cutoff). Raises ``ValueError`` on an unparseable value — the caller refuses
    to purge rather than deleting the wrong rows."""
    if before is None:
        return None
    if isinstance(before, datetime):
        dt = before
    else:
        dt = datetime.fromisoformat(str(before).strip())  # ValueError on junk
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)  # naive input = UTC (the stored convention)
    return dt.astimezone(UTC).isoformat()


# Preview length in the hot-write event payload — enough for a console toast /
# Activity line to be meaningful, small enough to never bloat the bus.
_HOT_EVENT_PREVIEW_CHARS = 160


def _publish_hot_write(chunk_id: int, source: str | None, source_type: str | None, content: str) -> None:
    """Emit ``memory.hot_written`` on the plugin event bus (ADR 0069 D8).

    ``domain="hot"`` chunks are injected in front of the model EVERY turn, so a
    write to that domain must be visible, not silent — this is how the console
    notification path (and any ADR 0039 subscriber) learns one landed, whoever
    wrote it (agent tool, operator route, plugin SDK). Best-effort via the
    late-bound ``HOST.publish`` seam (same pattern as ``tasks/store.py``): a
    missing bus (unit tests, standalone use) or a bus hiccup must never break
    a store write. The lazy import keeps ``knowledge`` free of a hard ``graph``
    dependency."""
    try:
        from graph.plugins.host import HOST

        if HOST.publish:
            HOST.publish(
                "memory.hot_written",
                {
                    "chunk_id": chunk_id,
                    "source": source or "",
                    "source_type": source_type or "",
                    "preview": (content or "")[:_HOT_EVENT_PREVIEW_CHARS],
                },
            )
    except Exception:  # noqa: BLE001 — visibility must never break a write
        log.debug("[knowledge] hot-write event publish failed", exc_info=True)


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


def _namespace_clause(namespace: str | list[str] | None, col: str = "namespace") -> tuple[str, list[str]]:
    """SQL predicate + params for a namespace filter (ADR 0069 D3a).

    Accepts one value or a list. The empty string ``""`` in the filter matches
    rows with NO namespace (NULL or ''), so scoped auto-inject can still include
    un-namespaced chunks — most rows written before namespaces were filtered.
    ``None`` / empty list → no predicate (unfiltered, today's behavior).
    """
    if namespace is None:
        return "", []
    values = [namespace] if isinstance(namespace, str) else [str(v) for v in namespace]
    if not values:
        return "", []
    named = [v for v in values if v != ""]
    arms: list[str] = []
    params: list[str] = []
    if named:
        arms.append(f"{col} IN ({','.join('?' for _ in named)})")
        params.extend(named)
    if len(named) != len(values):  # "" requested → match un-namespaced rows
        arms.append(f"({col} IS NULL OR {col} = '')")
    return "(" + " OR ".join(arms) + ")", params


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


# Discriminator stamped on ``invalidation_reason`` by :meth:`KnowledgeStore.invalidate_by_source`
# (the #1770 bulk soft-delete). It's what lets :meth:`KnowledgeStore.purge_invalidated`
# reap ONLY bulk-deleted ingests once they age past the grace window, while leaving
# auto-supersession audit rows (ADR 0069 D9, which stamp ``invalidated_at`` with a NULL
# reason and are kept forever) untouched. Both paths set ``invalidated_at``, so without
# this marker they'd be indistinguishable and the sweep would wipe supersession history.
_BULK_DELETE_REASON = "source_delete"

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
    epoch         TEXT,
    created_at    TEXT NOT NULL,
    updated_at    TEXT NOT NULL,
    invalidated_at TEXT,
    invalidation_reason TEXT
);

CREATE INDEX IF NOT EXISTS idx_chunks_domain     ON chunks(domain);
CREATE INDEX IF NOT EXISTS idx_chunks_created_at ON chunks(created_at);
CREATE INDEX IF NOT EXISTS idx_chunks_source     ON chunks(source);

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
            # Migration: add the invalidated_at column (ADR 0069 D9 — supersede,
            # don't delete). Same additive+nullable pattern as namespace; NULL =
            # the row is valid, an ISO timestamp = superseded by a newer row.
            try:
                cols = {r[1] for r in db.execute("PRAGMA table_info(chunks)")}
                if "invalidated_at" not in cols:
                    db.execute("ALTER TABLE chunks ADD COLUMN invalidated_at TEXT")
                db.execute("CREATE INDEX IF NOT EXISTS idx_chunks_invalidated_at ON chunks(invalidated_at)")
            except sqlite3.DatabaseError as exc:
                log.debug("[knowledge] invalidated_at migration skipped: %s", exc)
            # Migration: add the invalidation_reason column (#1770 — bulk soft delete).
            # Same additive+nullable pattern. NULL means "invalidated but kept for audit"
            # (auto-supersession, ADR 0069 D9); ``_BULK_DELETE_REASON`` marks a bulk
            # delete-by-source so the grace sweep reaps only those. Rows soft-deleted on a
            # pre-migration build carry a NULL reason and are therefore preserved by the
            # sweep — the safe (never-lose-audit-history) default.
            try:
                cols = {r[1] for r in db.execute("PRAGMA table_info(chunks)")}
                if "invalidation_reason" not in cols:
                    db.execute("ALTER TABLE chunks ADD COLUMN invalidation_reason TEXT")
            except sqlite3.DatabaseError as exc:
                log.debug("[knowledge] invalidation_reason migration skipped: %s", exc)
            # Migration: add the epoch column (#1634 — knowledge lifecycle). Same
            # additive+nullable pattern; an epoch tag ("2026-06-29") scopes a chunk
            # to one era of a resettable world, so a wipe is a new tag rather than
            # a delete — old lessons stay for post-mortems but stop matching an
            # epoch-filtered search.
            try:
                cols = {r[1] for r in db.execute("PRAGMA table_info(chunks)")}
                if "epoch" not in cols:
                    db.execute("ALTER TABLE chunks ADD COLUMN epoch TEXT")
                db.execute("CREATE INDEX IF NOT EXISTS idx_chunks_epoch ON chunks(epoch)")
            except sqlite3.DatabaseError as exc:
                log.debug("[knowledge] epoch migration skipped: %s", exc)
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
        epoch: str | None = None,
    ) -> int | None:
        """Insert a chunk. Returns the new row id, or None on failure.

        Every write funnels through here, so this is where the ADR 0021
        guardrail lives: the model's internal reasoning must never reach the
        store. We strip ``<scratch_pad>``/``<think>`` defensively — covering all
        writers (memory tools, ingest, harvest, future ones), not just the ones
        that remember to clean their input.

        ``epoch`` (#1634) tags the chunk with the era it was learned in (an
        opaque string, e.g. a reset date) so ``search(epoch=...)`` can scope
        retrieval to the current era of a resettable world.
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
                "namespace, epoch, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (content, domain, heading, source, source_type, finding_type, namespace, epoch, now, now),
            )
            db.commit()
            chunk_id = int(cur.lastrowid)
            # Hot-memory write visibility (ADR 0069 D8): every write funnels
            # through here, so this one hook covers every writer of the
            # always-on domain — agent tool, operator routes, plugin SDK.
            if domain == "hot":
                _publish_hot_write(chunk_id, source, source_type, content)
            return chunk_id
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
        epoch: str | None = None,
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
                epoch=epoch,
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
        namespace: str | list[str] | None = None,
        include_invalidated: bool = False,
        epoch: str | None = None,
    ) -> list[dict[str, Any]]:
        """Top-k chunks matching ``query``. Shape matches what the
        ``KnowledgeMiddleware`` consumes: each result has ``table``,
        ``preview``, plus the underlying chunk fields.

        Uses FTS5 when available, else a tokenized LIKE fallback. Returns
        an empty list on no matches or DB failure (never raises).
        ``namespace`` (ADR 0069 D3a) optionally restricts hits to the given
        namespace value(s) — see :func:`_namespace_clause` for the ``""``
        (un-namespaced rows) convention. ``None`` = unfiltered.

        Superseded rows (``invalidated_at`` set — ADR 0069 D9) are excluded
        by default; ``include_invalidated=True`` is the escape hatch for
        audit tooling that needs the full history.

        ``epoch`` (#1634) restricts hits to chunks tagged with exactly that
        epoch (see :meth:`add_chunk`) — chunks from other eras, and untagged
        chunks, don't match. ``None`` = unfiltered (today's behavior).
        """
        if not query or not query.strip():
            return []
        db = self._get_db()
        if db is None:
            return []
        try:
            rows = (
                self._search_fts(db, query, k, domain, namespace, include_invalidated, epoch)
                if self._fts_available
                else self._search_like(db, query, k, domain, namespace, include_invalidated, epoch)
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
        namespace: str | list[str] | None = None,
        include_invalidated: bool = False,
        epoch: str | None = None,
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
        where = ["chunks_fts MATCH ?"]
        params: list[Any] = [match]
        if not include_invalidated:
            where.append("c.invalidated_at IS NULL")
        if domain:
            where.append("c.domain = ?")
            params.append(domain)
        if epoch:
            where.append("c.epoch = ?")
            params.append(epoch)
        ns_sql, ns_params = _namespace_clause(namespace, col="c.namespace")
        if ns_sql:
            where.append(ns_sql)
            params.extend(ns_params)
        params.append(k)
        return db.execute(
            "SELECT c.* FROM chunks_fts f "
            "JOIN chunks c ON c.id = f.rowid "
            f"WHERE {' AND '.join(where)} "
            "ORDER BY rank LIMIT ?",
            params,
        ).fetchall()

    def _search_like(
        self,
        db: sqlite3.Connection,
        query: str,
        k: int,
        domain: str | None,
        namespace: str | list[str] | None = None,
        include_invalidated: bool = False,
        epoch: str | None = None,
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
        if not include_invalidated:
            sql += " AND invalidated_at IS NULL"
        if domain:
            sql += " AND domain = ?"
            params.append(domain)
        if epoch:
            sql += " AND epoch = ?"
            params.append(epoch)
        ns_sql, ns_params = _namespace_clause(namespace)
        if ns_sql:
            sql += f" AND {ns_sql}"
            params.extend(ns_params)
        sql += " ORDER BY score DESC, id DESC LIMIT ?"
        params.append(k)
        return db.execute(sql, params).fetchall()

    def list_chunks(
        self,
        domain: str | None = None,
        limit: int = 50,
        *,
        namespace: str | None = None,
        include_invalidated: bool = False,
    ) -> list[Chunk]:
        """Most-recent-first chunk listing. Used by ``memory_list`` and the
        fact consolidator. ``namespace`` (ADR 0021) optionally scopes to one
        per-project/owner bucket.

        Superseded rows (ADR 0069 D9) are excluded by default — so hot-memory
        injection, ``memory_list``, and the fact consolidator only see valid
        rows. ``include_invalidated=True`` is the audit escape hatch."""
        db = self._get_db()
        if db is None:
            return []
        clauses: list[str] = []
        params: list[Any] = []
        if not include_invalidated:
            clauses.append("invalidated_at IS NULL")
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
        """HARD-delete one chunk by id. This is the operator-intent path
        (``forget_memory``, the inspector's DELETE routes) — an explicit
        delete removes the row outright, history-keeping notwithstanding.
        Automatic supersession uses :meth:`invalidate_chunk` instead
        (ADR 0069 D9). Returns True if a row was removed."""
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

    def invalidate_chunk(self, chunk_id: int) -> bool:
        """Mark one chunk superseded (ADR 0069 D9): set ``invalidated_at`` to
        now, keeping the row for audit/history. Invalidated rows drop out of
        ``search``/``list_chunks``/hot memory by default but stay reachable via
        the ``include_invalidated`` escape hatch. Idempotent-safe: returns True
        only when a VALID row was invalidated (already-invalidated or unknown
        ids return False)."""
        db = self._get_db()
        if db is None:
            return False
        try:
            now = _now_iso()
            cur = db.execute(
                "UPDATE chunks SET invalidated_at = ?, updated_at = ? WHERE id = ? AND invalidated_at IS NULL",
                (now, now, int(chunk_id)),
            )
            db.commit()
            return cur.rowcount > 0
        except sqlite3.DatabaseError:
            log.exception("[knowledge] invalidate_chunk failed")
            return False
        finally:
            db.close()

    def get_hot_memory_entries(self, max_chars: int = 6000) -> list[tuple[int, str]]:
        """The ``(chunk_id, formatted piece)`` pairs behind :meth:`get_hot_memory`.

        Id-attributed so the per-turn injection record (ADR 0069 D6) can name
        exactly which hot chunks entered a model call. Same selection/budget
        semantics as ``get_hot_memory`` (one source of truth — it joins this).
        Superseded chunks never inject: ``list_chunks`` excludes
        ``invalidated_at`` rows by default (ADR 0069 D9).
        """
        chunks = self.list_chunks(domain="hot", limit=100)  # newest-first, valid-only
        entries: list[tuple[int, str]] = []
        total = 0
        for c in chunks:  # newest-first → oldest trimmed when over budget
            piece = (f"[{c.heading}] " if c.heading else "") + c.content
            if total + len(piece) > max_chars:
                break
            entries.append((c.id, piece))
            total += len(piece)
        return entries

    def get_hot_memory(self, max_chars: int = 6000) -> str:
        """Concatenate every ``domain="hot"`` chunk for always-on injection.

        "Hot" chunks are operator facts that should be in front of the model
        every turn (vs. retrieved-on-relevance). ``KnowledgeMiddleware`` reads
        this each turn so a newly-added hot fact is seen immediately. Returns
        "" when there are none; trims oldest-first if over ``max_chars``.
        """
        return "\n".join(piece for _, piece in self.get_hot_memory_entries(max_chars))

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

    def purge_domain(self, domain: str, *, before: str | datetime | None = None) -> int:
        """HARD-delete every chunk in ``domain`` — optionally only those created
        strictly before ``before`` (an ISO-8601 timestamp or a datetime; naive =
        UTC). The knowledge-lifecycle primitive (#1634): a long-running plugin
        retires a whole bucket of now-wrong lessons (or just the old ones) in one
        call. Returns the count removed.

        Explicit-intent path like :meth:`delete_by_id` — rows are removed
        outright (the FTS delete trigger keeps ``chunks_fts`` in sync); for
        keep-for-audit retirement, tag writes with ``epoch`` instead. An empty
        ``domain`` or an unparseable ``before`` refuses to purge (returns 0)
        rather than risk deleting the wrong rows."""
        if not domain or not domain.strip():
            return 0
        try:
            cutoff = _normalize_before(before)
        except ValueError:
            log.warning("[knowledge] purge_domain(%r): unparseable before=%r — refusing to purge", domain, before)
            return 0
        db = self._get_db()
        if db is None:
            return 0
        try:
            sql = "DELETE FROM chunks WHERE domain = ?"
            params: list[Any] = [domain]
            if cutoff is not None:
                sql += " AND created_at < ?"
                params.append(cutoff)
            cur = db.execute(sql, params)
            db.commit()
            return int(cur.rowcount)
        except sqlite3.DatabaseError as exc:
            log.warning("[knowledge] purge_domain failed: %s", exc)
            return 0
        finally:
            db.close()

    # ── bulk delete-by-source lifecycle (#1770) ─────────────────────────────
    # Deleting a whole ingest one chunk at a time is impractical, so the console
    # groups chunks by source and bulk-deletes them. It's a SOFT delete: the rows
    # are invalidated (drop out of search/list/hot-memory at once, ADR 0069 D9) but
    # survive a grace window so an Undo — restore_by_source — can bring them back.
    # purge_invalidated is the sweep that eventually makes the removal permanent.

    def invalidate_by_source(self, source: str) -> int:
        """SOFT-delete every VALID chunk sharing ``source`` — stamp
        ``invalidated_at`` so the whole ingest leaves recall at once while the rows
        survive for a grace window. Backs the console's bulk "delete all chunks
        from this source" (#1770); reversible via :meth:`restore_by_source`, made
        permanent by :meth:`purge_invalidated`. Empty/whitespace ``source`` is a
        no-op (0) — never an ``invalidated_at = everything`` sweep. Returns the
        count newly invalidated (already-invalidated rows aren't recounted).

        Stamps ``invalidation_reason = _BULK_DELETE_REASON`` alongside
        ``invalidated_at`` so :meth:`purge_invalidated` can tell a reapable bulk
        soft-delete apart from an auto-supersession audit row (ADR 0069 D9), which
        leaves the reason NULL and is kept forever."""
        if not source or not source.strip():
            return 0
        db = self._get_db()
        if db is None:
            return 0
        try:
            now = _now_iso()
            cur = db.execute(
                "UPDATE chunks SET invalidated_at = ?, invalidation_reason = ?, updated_at = ? "
                "WHERE source = ? AND invalidated_at IS NULL",
                (now, _BULK_DELETE_REASON, now, source),
            )
            db.commit()
            return int(cur.rowcount)
        except sqlite3.DatabaseError as exc:
            log.warning("[knowledge] invalidate_by_source failed: %s", exc)
            return 0
        finally:
            db.close()

    def restore_by_source(self, source: str) -> int:
        """Clear ``invalidated_at`` on every BULK-SOFT-DELETED chunk sharing
        ``source`` — the inverse of :meth:`invalidate_by_source`, backing the
        console's Undo toast (#1770). Only rows still present (not yet swept by
        :meth:`purge_invalidated`) can be restored. Empty ``source`` is a no-op.
        Returns the count restored.

        Scoped to ``invalidation_reason = _BULK_DELETE_REASON`` so an Undo can only
        resurrect what THIS feature soft-deleted — never an auto-supersession audit
        row (ADR 0069 D9) that happens to share the ``source`` string. Clears the
        reason marker on the way back out."""
        if not source or not source.strip():
            return 0
        db = self._get_db()
        if db is None:
            return 0
        try:
            cur = db.execute(
                "UPDATE chunks SET invalidated_at = NULL, invalidation_reason = NULL, updated_at = ? "
                "WHERE source = ? AND invalidation_reason = ?",
                (_now_iso(), source, _BULK_DELETE_REASON),
            )
            db.commit()
            return int(cur.rowcount)
        except sqlite3.DatabaseError as exc:
            log.warning("[knowledge] restore_by_source failed: %s", exc)
            return 0
        finally:
            db.close()

    def _invalidated_cutoff(self, older_than_seconds: int) -> str:
        """The ``invalidated_at`` boundary for :meth:`purge_invalidated`: rows
        stamped at or before this UTC-ISO instant are past the grace window. Shared
        with the hybrid override so both delete under ONE cutoff (no orphan-vector
        window from a second ``now()``)."""
        return (datetime.now(UTC) - timedelta(seconds=max(0, int(older_than_seconds)))).isoformat()

    def purge_invalidated(self, older_than_seconds: int = 0, *, _cutoff: str | None = None) -> int:
        """HARD-delete BULK-SOFT-DELETED chunks whose ``invalidated_at`` is at or
        older than ``older_than_seconds`` ago — the grace sweep that makes a bulk
        delete-by-source (#1770) eventually permanent.

        Reaps ONLY rows stamped ``invalidation_reason = _BULK_DELETE_REASON`` by
        :meth:`invalidate_by_source`. Auto-supersession rows (ADR 0069 D9, which set
        ``invalidated_at`` with a NULL reason) are deliberately excluded and kept
        forever as audit history — the ``include_invalidated`` escape hatch stays
        populated. Without this reason filter the sweep would silently wipe that
        supersession history on the first bulk delete.

        ``older_than_seconds<=0`` purges every bulk-soft-deleted row now. Returns the
        count removed.

        The FTS delete trigger keeps ``chunks_fts`` in sync; a hybrid store
        overrides this to drop the side-table vectors first (no FK cascade).
        ``_cutoff`` is an internal seam for that override to share one boundary."""
        db = self._get_db()
        if db is None:
            return 0
        try:
            cutoff = _cutoff if _cutoff is not None else self._invalidated_cutoff(older_than_seconds)
            cur = db.execute(
                "DELETE FROM chunks "
                "WHERE invalidated_at IS NOT NULL AND invalidated_at <= ? AND invalidation_reason = ?",
                (cutoff, _BULK_DELETE_REASON),
            )
            db.commit()
            return int(cur.rowcount)
        except sqlite3.DatabaseError as exc:
            log.warning("[knowledge] purge_invalidated failed: %s", exc)
            return 0
        finally:
            db.close()
