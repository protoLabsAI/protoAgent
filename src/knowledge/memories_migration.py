"""ADR 0125 P2-B (D6): the boot migration draining durable memories out of the
legacy ``chunks`` table into the ADR 0125 ``memories`` table (same core agent.db).

It is the memory sibling of :func:`knowledge.store.migrate_conversation_chunks_to_history`
(H1e) and carries the same rules: a ``_kb_meta`` stamp that records a pass but never
skips the probe (so a row an older core writes later still moves on a later run); a
one-time file-level backup taken *before* the first move; and the whole move — insert
into ``memories`` (+``memories_fts`` via trigger, +``memory_vectors``) and delete from
``chunks`` (+``chunks_fts`` via trigger, +``chunk_vectors``) — in ONE transaction.

Routing deliberately refines the ADR's domain table (decided 2026-11-07 after a fleet
audit): live agents saved memories into freeform domains (``applications``, ``research``,
``voice``, ``loop-lessons``, ``killteam``), so routing by domain name would misfile them.
A row is a durable memory iff its ``source_type`` is one the agent/operator write paths
stamp (``conversation``/``extracted``/``operator``); everything else — ingested/url/pdf/
youtube/text/file/background content and the NULL-``source_type`` ``claude-import`` rows —
stays in ``chunks``, now tagged with a ``collection`` (= its old ``domain``). The
``domain='conversation'`` rows are left untouched: the H1e history migration owns those.

This card does NOT wire the boot call — the switch-over card (P2-D1) adds it in the same
PR that points the memory tools at ``memories``, so a build of main never has memory split
across two stores.
"""

from __future__ import annotations

import logging
import shutil
import sqlite3
from pathlib import Path

from knowledge.store import (
    HISTORY_DOMAIN,
    REVIEW_STATES,
    SUPERSEDED_BY_PREFIX,
    _now_iso,
    _table_present,
    infer_review_state,
    superseded_by_id,
)

log = logging.getLogger(__name__)

# ``_kb_meta`` key stamped once a pass finishes (never read to SKIP — see the
# module docstring; the real idempotency is that moved rows are gone).
_MEMORY_MIGRATION_KEY = "memory_migration_0125"

# Phase-2 backup suffix. Deliberately NOT ``.pre-0125`` — that file is the
# Phase-1 (history) backup and must never be overwritten here.
_PRE_0125_P2_BACKUP_SUFFIX = ".pre-0125-p2"

# ``chunks.source_type`` values the agent/operator write paths stamp, i.e. the
# rows that are durable memories. NULL and every ingest/third-party type stays.
_MEMORY_SOURCE_TYPES = ("conversation", "extracted", "operator")

# The ``memories`` ``kind`` CHECK set — a ``memory_kind`` already in it is carried
# verbatim; otherwise ``domain`` decides (preferences→profile, decisions→decision,
# else the generic ``fact``).
_MEMORY_KINDS = ("profile", "standing", "fact", "decision")

# Rows that are memories: a durable source_type AND not the history domain (the
# history migration owns ``domain='conversation'`` rows, whatever their source_type).
_MEMORY_PREDICATE = "domain != ? AND source_type IN ({})".format(", ".join("?" * len(_MEMORY_SOURCE_TYPES)))
_MEMORY_PARAMS = (HISTORY_DOMAIN, *_MEMORY_SOURCE_TYPES)

_ZERO_RESULT = {"memories": 0, "chunks": 0}


def _zero() -> dict[str, int]:
    return dict(_ZERO_RESULT)


def _memory_kind(memory_kind: str | None, domain: str | None) -> str:
    """The ``memories.kind`` for a chunk: its ``memory_kind`` if already one of the
    four typed kinds; else ``preferences``→profile / ``decisions``→decision; else ``fact``."""
    if memory_kind in _MEMORY_KINDS:
        return memory_kind
    if domain == "preferences":
        return "profile"
    if domain == "decisions":
        return "decision"
    return "fact"


def _memory_delivery(domain: str | None, delivery_policy: str | None) -> str:
    """The ``memories.delivery`` for a chunk: ``always`` for a hot row or an
    explicit always policy; ``on_demand`` for that policy; else ``retrieved``."""
    if domain == "hot" or delivery_policy == "always":
        return "always"
    if delivery_policy == "on_demand":
        return "on_demand"
    return "retrieved"


def _memory_provenance(source_type: str | None) -> str:
    """The ``memories.provenance`` for a chunk, from its ``source_type``: operator→operator,
    extracted→extracted, conversation (the agent's own writes)→agent. Routing only admits
    those three, so an unexpected value defaults to the agent tier."""
    if source_type == "operator":
        return "operator"
    if source_type == "extracted":
        return "extracted"
    return "agent"


def _memory_review_state(review_state: str | None, source_type: str | None) -> str:
    """Carry a chunk's ``review_state`` over verbatim; a NULL (or any value the
    ``memories`` CHECK would reject) falls back to the trust-tier rule
    (:func:`knowledge.store.infer_review_state` — operator rows confirmed, else pending)."""
    if review_state in REVIEW_STATES:
        return review_state
    return infer_review_state(source_type)


# Columns read off each memories-bound chunk (order mirrors ``_insert_memory``).
_SELECT_COLS = (
    "id, content, domain, heading, source, source_type, namespace, memory_kind, "
    "subject, review_state, expires_at, delivery_policy, invalidated_at, "
    "invalidation_reason, created_at, updated_at"
)


def _insert_memory(db: sqlite3.Connection, row: sqlite3.Row) -> int:
    """Insert one chunk into ``memories`` and return the new row id. The ``memories_ai``
    trigger populates ``memories_fts``; ``superseded_by`` is left NULL here and set in a
    second pass once every old→new id is known. A module-level seam so a test can inject a
    mid-transaction failure and assert the whole move rolls back."""
    cur = db.execute(
        "INSERT INTO memories (kind, subject, heading, content, delivery, review_state, "
        "provenance, scope, legacy_domain, expires_at, source_session, invalidated_at, "
        "invalidation_reason, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            _memory_kind(row["memory_kind"], row["domain"]),
            row["subject"],
            row["heading"],
            row["content"],
            _memory_delivery(row["domain"], row["delivery_policy"]),
            _memory_review_state(row["review_state"], row["source_type"]),
            _memory_provenance(row["source_type"]),
            row["namespace"],  # namespace → scope
            row["domain"],  # old domain → legacy_domain
            row["expires_at"],
            row["source"],  # source → source_session
            row["invalidated_at"],
            row["invalidation_reason"],  # rewritten in pass 2 if it names a migrated chunk
            row["created_at"],
            row["updated_at"],
        ),
    )
    return int(cur.lastrowid)


def _backup_pre_0125_p2(db: sqlite3.Connection, path: Path) -> None:
    """Copy agent.db to ``agent.db.pre-0125-p2`` before the first move. Checkpoint WAL
    first so the copy reflects all committed writes. Taken ONCE: a later pass (rows an
    older core wrote after the stamp) keeps the ORIGINAL snapshot, and the Phase-1
    ``.pre-0125`` backup is a different file and never touched."""
    try:
        db.execute("PRAGMA wal_checkpoint(FULL)")
    except sqlite3.DatabaseError:
        pass  # non-WAL / read-only mount: the main file is already current
    backup = path.with_name(path.name + _PRE_0125_P2_BACKUP_SUFFIX)
    if backup.exists():
        return
    shutil.copy2(path, backup)


def migrate_memory_chunks_to_memories(db_path: str | Path | None) -> dict[str, int]:
    """Move durable memories from ``chunks`` into ``memories`` in the same core agent.db
    (ADR 0125 D6 / P2-B). Returns counts per destination: ``{"memories": moved,
    "chunks": collection-backfilled}``.

    Routing (see the module docstring): ``domain='conversation'`` is skipped (H1e owns it);
    a ``source_type`` of conversation/extracted/operator moves to ``memories``; everything
    else stays in ``chunks`` and is tagged with a ``collection`` (= its old ``domain``).
    Vectors are copied verbatim (``chunk_vectors`` → ``memory_vectors``), never re-embedded.
    A supersede pointer (``invalidation_reason`` naming a migrated chunk) is rewritten to the
    new ``memories`` id and ``superseded_by`` set, using an old→new map built in the same
    transaction.

    Idempotent like H1e: the ``_kb_meta`` stamp records a finished pass but the probe runs
    every time, so a memory an older core writes after the stamp still moves on the next run.
    The insert (+fts, +vectors) and the delete (+fts, +vectors) run in ONE transaction; the
    deletes are a single predicate each (no 3k-long IN list). Core agent.db only: a file with
    no ``chunks`` (a plugin KnowledgeBackend) or no ``memories`` table is a no-op.
    """
    if db_path is None:
        return _zero()
    path = Path(db_path)
    if not path.exists():
        return _zero()
    try:
        db = sqlite3.connect(str(path))
    except sqlite3.DatabaseError:
        log.exception("[knowledge] 0125 memory migration: cannot open %s", path)
        return _zero()
    db.row_factory = sqlite3.Row
    try:
        db.execute("PRAGMA busy_timeout=5000")
        # A DB with no ``_kb_meta`` predates the chunks schema — nothing to migrate. The
        # stamp (written below) records a finished pass but does NOT skip the probe: an
        # older core sharing this file keeps writing memory chunks after the stamp, and
        # those still move on the next run.
        try:
            db.execute("SELECT value FROM _kb_meta WHERE key = ?", (_MEMORY_MIGRATION_KEY,)).fetchone()
        except sqlite3.DatabaseError:
            return _zero()
        # Both tables must exist: the switch-over caller runs this only once the MemoryStore
        # has created ``memories``; a core file with no ``chunks`` (plugin backend) is empty.
        if not _table_present(db, "chunks") or not _table_present(db, "memories"):
            return _zero()

        # ``collection`` column (idempotent ALTER). Additive + nullable, committed before the
        # move so the backfill below can populate it. Non-destructive, so it needs no backup.
        cols = {r[1] for r in db.execute("PRAGMA table_info(chunks)")}
        if "collection" not in cols:
            db.execute("ALTER TABLE chunks ADD COLUMN collection TEXT")
            db.commit()

        rows = db.execute(f"SELECT {_SELECT_COLS} FROM chunks WHERE {_MEMORY_PREDICATE}", _MEMORY_PARAMS).fetchall()
        has_vectors = _table_present(db, "chunk_vectors")

        # File-level backup BEFORE any move (only when there is something to move — a
        # pure collection-backfill is additive). Taken once; never overwritten.
        if rows:
            _backup_pre_0125_p2(db, path)

        with db:  # one transaction: insert memories (+fts, +vectors), delete chunks (+fts, +vectors)
            old_to_new: dict[int, int] = {}
            for r in rows:
                old_to_new[int(r["id"])] = _insert_memory(db, r)
            # Supersede chain: a reason naming a migrated chunk is rewritten to the NEW
            # memories id and ``superseded_by`` set. Second pass — the target's new id may
            # not have existed yet during the first. Touches only audit columns, so the
            # ``memories_au`` (heading/content) trigger does not fire and vectors survive.
            for r in rows:
                old_target = superseded_by_id(r["invalidation_reason"])
                if old_target is not None and old_target in old_to_new:
                    new_target = old_to_new[old_target]
                    db.execute(
                        "UPDATE memories SET superseded_by = ?, invalidation_reason = ? WHERE id = ?",
                        (new_target, f"{SUPERSEDED_BY_PREFIX}{new_target}", old_to_new[int(r["id"])]),
                    )
            # Copy vectors verbatim (no re-embed). One SELECT by predicate (dodging the
            # host-parameter cap) mapped through old→new, one executemany.
            if has_vectors and rows:
                vec_rows = db.execute(
                    f"SELECT chunk_id, vec FROM chunk_vectors WHERE chunk_id IN "
                    f"(SELECT id FROM chunks WHERE {_MEMORY_PREDICATE})",
                    _MEMORY_PARAMS,
                ).fetchall()
                pairs = [(old_to_new[int(v["chunk_id"])], v["vec"]) for v in vec_rows if int(v["chunk_id"]) in old_to_new]
                if pairs:
                    db.executemany("INSERT INTO memory_vectors (memory_id, vec) VALUES (?, ?)", pairs)
            # Delete the moved chunks and their side rows by predicate, not a 3k-long IN
            # list. Vectors first (their subquery reads ``chunks``), then the chunks (the
            # ``chunks_ad`` trigger clears ``chunks_fts``).
            if rows:
                if has_vectors:
                    db.execute(
                        f"DELETE FROM chunk_vectors WHERE chunk_id IN (SELECT id FROM chunks WHERE {_MEMORY_PREDICATE})",
                        _MEMORY_PARAMS,
                    )
                db.execute(f"DELETE FROM chunks WHERE {_MEMORY_PREDICATE}", _MEMORY_PARAMS)
            # Tag every retained (non-history) chunk with its collection. NULL-only so a
            # re-run is a no-op and a row an older core writes later is tagged next run.
            backfilled = db.execute(
                "UPDATE chunks SET collection = domain WHERE collection IS NULL AND domain != ?",
                (HISTORY_DOMAIN,),
            ).rowcount
            db.execute(
                "INSERT INTO _kb_meta(key, value) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (_MEMORY_MIGRATION_KEY, _now_iso()),
            )
        if rows or backfilled:
            log.info(
                "[knowledge] 0125 memory migration: moved %d memory chunk(s) into memories, "
                "tagged %d retained chunk(s) with a collection%s",
                len(rows),
                backfilled,
                f" (backup: {path.name + _PRE_0125_P2_BACKUP_SUFFIX})" if rows else "",
            )
        return {"memories": len(rows), "chunks": int(backfilled)}
    except sqlite3.DatabaseError:
        log.exception("[knowledge] 0125 memory migration failed at %s", path)
        return _zero()
    finally:
        db.close()
