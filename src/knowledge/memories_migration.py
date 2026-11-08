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
``voice``, ``loop-lessons``, ``killteam``), so routing by domain name alone would misfile
them. The rule, per ``source_type``:

* ``conversation``/``extracted`` — the agent's own memory_ingest writes and the session-end
  fact extractor — are durable memories in whatever (freeform) domain they live in, so they
  move regardless of domain.
* ``operator`` is NOT a blanket move: the console stamps it on BOTH memory curation AND
  knowledge-base curation. POST ``/api/knowledge/chunks`` pastes documents as ``operator``
  and PUT ``/api/knowledge/chunks/{id}`` re-stamps ``operator`` on every edited chunk —
  including ingested pdf/url/youtube rows. Those are knowledge, not memory, and must stay in
  ``chunks`` (else knowledge search loses them and they resurface as confirmed ``fact``
  memories). An ``operator`` row is a memory only when it carries a memory signal the KB
  routes never produce: a canonical memory domain (``hot``/``preferences``/``decisions`` —
  what the Memory inspector curates) or ``delivery_policy='always'`` (a hot-pinned row,
  which may sit in a freeform domain). The memory tool's own writes come through as
  ``conversation``, so a freeform-domain agent memory still moves via the first rule.
* everything else — ingested/url/pdf/youtube/text/file/background content, operator-curated
  KB documents, and the NULL-``source_type`` ``claude-import`` rows — stays in ``chunks``,
  now tagged with a ``collection`` (= its old ``domain``).

``kind`` follows ``memory_kind`` when it is already one of the four typed kinds; an
untyped row in an operator-profile domain — ``preferences`` and the freeform ``profile``/
``abilities``/``voice`` the Career Coach plugin writes — becomes ``profile``. v0.231.0
missed the freeform three and filed them as ``fact``; :func:`repair_profile_memory_kinds`
re-kinds those rows on the next boot, keyed on the ``.pre-0125-p2`` snapshot.

The ``domain='conversation'`` rows are left untouched whatever their source_type: the H1e
history migration owns those.

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
    DELIVERY_ALWAYS,
    HISTORY_DOMAIN,
    REVIEW_STATES,
    SUPERSEDED_BY_PREFIX,
    _begin_immediate,
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

# ``source_type`` values written ONLY on the memory paths (the agent's memory_ingest
# tool stamps ``conversation``; session-end extraction stamps ``extracted``). These are
# durable memories wherever they live, so they move regardless of domain.
_AGENT_SOURCE_TYPES = ("conversation", "extracted")

# Canonical memory domains the Memory inspector curates (what an ``operator`` write means
# memory, not knowledge). ``preferences``→profile and ``decisions``→decision; ``hot`` is the
# always-on bucket. A hot-pinned memory may sit in a freeform domain, so ``delivery_policy=
# 'always'`` is admitted too (see ``_MEMORY_PREDICATE``). An ``operator`` row outside both —
# a pasted/edited knowledge-base document — stays in ``chunks``.
_OPERATOR_MEMORY_DOMAINS = ("hot", "preferences", "decisions")

# The ``memories`` ``kind`` CHECK set — a ``memory_kind`` already in it is carried
# verbatim; otherwise ``domain`` decides (preferences→profile, decisions→decision, an
# untyped row in an operator-profile domain→profile, else the generic ``fact``).
_MEMORY_KINDS = ("profile", "standing", "fact", "decision")

# Freeform domains live agents used as an operator-profile index (fleet audit, 2026-11-07):
# the Career Coach plugin's SOUL keeps "a compact recall index under the ``profile``,
# ``abilities`` and ``voice`` domains, distilled from the profile", and jobCoach's
# operator profile, abilities and voice rows sit there. ``infer_memory_kind`` had no
# branch for them, so they were stamped ``legacy`` (untyped). An untyped row there is
# about the operator → ``kind=profile``. A row the agent typed explicitly (``fact``,
# ``decision``, ``reference``, ...) keeps its own call: in jobCoach's ``profile`` domain
# those are research findings and positioning decisions, not the operator's profile.
_OPERATOR_PROFILE_DOMAINS = ("profile", "abilities", "voice")

# ``memory_kind`` values that mean "nobody typed this row": NULL (pre-#3205) and the
# ``legacy`` stamp ``infer_memory_kind`` gives an unmapped domain.
_UNTYPED_KINDS = (None, "legacy")

# Rows that are memories, NOT in the history domain (H1e owns ``domain='conversation'``):
# an agent/extraction write in any domain, OR an ``operator`` write that carries a memory
# signal a KB-curation write never produces — a canonical memory domain, or always-on
# delivery. Built once so the SELECT, the vector-copy subquery and both DELETEs share it.
_MEMORY_PREDICATE = (
    "domain != ? AND ("
    "source_type IN ({agents}) "
    "OR (source_type = 'operator' AND (domain IN ({opdoms}) OR delivery_policy = ?))"
    ")"
).format(
    agents=", ".join("?" * len(_AGENT_SOURCE_TYPES)),
    opdoms=", ".join("?" * len(_OPERATOR_MEMORY_DOMAINS)),
)
_MEMORY_PARAMS = (HISTORY_DOMAIN, *_AGENT_SOURCE_TYPES, *_OPERATOR_MEMORY_DOMAINS, DELIVERY_ALWAYS)

_ZERO_RESULT = {"memories": 0, "chunks": 0}


def _zero() -> dict[str, int]:
    return dict(_ZERO_RESULT)


def _memory_kind(memory_kind: str | None, domain: str | None) -> str:
    """The ``memories.kind`` for a chunk: its ``memory_kind`` if already one of the
    four typed kinds; else ``preferences``→profile / ``decisions``→decision; else an
    untyped row in an operator-profile domain (``profile``/``abilities``/``voice``)→profile;
    else ``fact``."""
    if memory_kind in _MEMORY_KINDS:
        return memory_kind
    if domain == "preferences":
        return "profile"
    if domain == "decisions":
        return "decision"
    if domain in _OPERATOR_PROFILE_DOMAINS and memory_kind in _UNTYPED_KINDS:
        return "profile"
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
    _backup_once(db, path, _PRE_0125_P2_BACKUP_SUFFIX)


def _backup_once(db: sqlite3.Connection, path: Path, suffix: str) -> None:
    """Copy agent.db to ``agent.db<suffix>`` unless that file already exists, after a
    WAL checkpoint so the copy reflects every committed write."""
    try:
        db.execute("PRAGMA wal_checkpoint(FULL)")
    except sqlite3.DatabaseError:
        pass  # non-WAL / read-only mount: the main file is already current
    backup = path.with_name(path.name + suffix)
    if backup.exists():
        return
    shutil.copy2(path, backup)


def migrate_memory_chunks_to_memories(db_path: str | Path | None) -> dict[str, int]:
    """Move durable memories from ``chunks`` into ``memories`` in the same core agent.db
    (ADR 0125 D6 / P2-B). Returns counts per destination: ``{"memories": moved,
    "chunks": collection-backfilled}``.

    Routing (see the module docstring): ``domain='conversation'`` is skipped (H1e owns it);
    a ``conversation``/``extracted`` write moves to ``memories`` whatever its domain, and an
    ``operator`` write moves only when memory-shaped (a ``hot``/``preferences``/``decisions``
    domain or ``delivery_policy='always'``) so operator-curated knowledge-base documents stay
    put; everything else stays in ``chunks`` and is tagged with a ``collection`` (= its old
    ``domain``). Vectors are copied verbatim (``chunk_vectors`` → ``memory_vectors``), never
    re-embedded.
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

        has_vectors = _table_present(db, "chunk_vectors")

        # File-level backup BEFORE the move, outside any transaction so its WAL checkpoint
        # runs cleanly — gated on a cheap probe so a pure collection-backfill (additive) does
        # not take one. A row an older core commits between this probe and the lock below is
        # still read + moved under the lock (it is just not in the snapshot) — never lost.
        if db.execute(f"SELECT EXISTS(SELECT 1 FROM chunks WHERE {_MEMORY_PREDICATE})", _MEMORY_PARAMS).fetchone()[0]:
            _backup_pre_0125_p2(db, path)

        # ONE transaction, and — unlike H1e, whose conversation writers had already moved —
        # an older core may still ``add_chunk`` a memory here. So take the write lock BEFORE
        # reading the rows to move: BEGIN IMMEDIATE freezes the set, so the DELETE-by-predicate
        # (card rule: a predicate, never a 3k-long IN list) removes exactly what the SELECT
        # read. Without it, a row committed between SELECT and DELETE is deleted un-migrated.
        _begin_immediate(db)
        rows = db.execute(f"SELECT {_SELECT_COLS} FROM chunks WHERE {_MEMORY_PREDICATE}", _MEMORY_PARAMS).fetchall()
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
        # ``chunks_ad`` trigger clears ``chunks_fts``). Same predicate + same lock as the
        # SELECT, so the set is identical.
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
        db.commit()  # commit the move + backfill + stamp atomically
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
        # Roll the (possibly half-applied) move back explicitly; the close() below would
        # also unwind it, but being explicit keeps the on-disk tables, FTS and vectors as
        # they were before this run — the all-or-nothing guarantee the tests assert.
        try:
            db.rollback()
        except sqlite3.DatabaseError:
            pass
        log.exception("[knowledge] 0125 memory migration failed at %s", path)
        return _zero()
    finally:
        db.close()


# ── repair: profile-domain rows an earlier core migrated as ``fact`` ─────────────────────

# ``_kb_meta`` key stamped once the repair has run. Unlike the move above, this stamp DOES
# skip: the repair is a one-shot correction of rows v0.231.0 migrated, and an operator who
# re-kinds a row after it ran must not have that undone on the next boot.
_PROFILE_KIND_REPAIR_KEY = "memory_profile_kind_repair_0125"

# File backup taken before the repair writes anything (never overwrites an earlier one).
_PROFILE_KIND_REPAIR_BACKUP_SUFFIX = ".pre-0125-kind-repair"


def repair_profile_memory_kinds(db_path: str | Path | None) -> int:
    """Re-kind memories that v0.231.0's migration filed as ``fact`` although they were
    untyped operator-profile rows (``legacy``/NULL ``memory_kind`` in a ``profile``/
    ``abilities``/``voice`` domain — what :func:`_memory_kind` now maps to ``profile``).
    Returns the number of rows re-kinded.

    Conservative by construction. A migrated row no longer records its original
    ``memory_kind`` (a ``legacy`` row and an explicitly typed ``reference`` both landed
    as ``fact``), so the record of truth is the migration's own pre-move snapshot,
    ``agent.db.pre-0125-p2``: a row is re-kinded only when that snapshot holds a chunk
    the migration would move, in one of those domains, untyped, AND a ``memories`` row
    still matches it exactly — same ``legacy_domain``, provenance, content, heading and
    both timestamps, still ``kind='fact'``. A row edited since the move (its
    ``updated_at`` changed), an explicitly typed row, or one with no snapshot is left
    alone. No content is interpreted.

    Idempotent: a ``_kb_meta`` stamp makes it one-shot, and the ``kind='fact'`` guard
    means a re-run would change nothing anyway. Before the first write the file is copied
    to ``agent.db.pre-0125-kind-repair``. A DB with no ``memories`` table, no migration
    stamp or no snapshot is a no-op."""
    if db_path is None:
        return 0
    path = Path(db_path)
    snapshot = path.with_name(path.name + _PRE_0125_P2_BACKUP_SUFFIX)
    if not path.exists():
        return 0
    try:
        db = sqlite3.connect(str(path))
    except sqlite3.DatabaseError:
        log.exception("[knowledge] 0125 profile-kind repair: cannot open %s", path)
        return 0
    db.row_factory = sqlite3.Row
    try:
        db.execute("PRAGMA busy_timeout=5000")
        if not _table_present(db, "_kb_meta") or not _table_present(db, "memories"):
            return 0
        meta = dict(db.execute("SELECT key, value FROM _kb_meta").fetchall())
        if _PROFILE_KIND_REPAIR_KEY in meta or _MEMORY_MIGRATION_KEY not in meta:
            # Already repaired, or not migrated yet (the move itself will type it right).
            return 0
        candidates = _snapshot_profile_candidates(snapshot) if snapshot.exists() else []
        # Each candidate is the exact identity of one moved chunk; ``kind='fact'`` keeps
        # the update to rows still as the migration left them.
        match_sql = (
            "kind = 'fact' AND legacy_domain = ? AND provenance = ? AND content = ? "
            "AND heading IS ? AND created_at = ? AND updated_at = ?"
        )
        params = [
            (
                c["domain"],
                _memory_provenance(c["source_type"]),
                c["content"],
                c["heading"],
                c["created_at"],
                c["updated_at"],
            )
            for c in candidates
        ]
        if any(db.execute(f"SELECT 1 FROM memories WHERE {match_sql}", p).fetchone() for p in params):
            _backup_once(db, path, _PROFILE_KIND_REPAIR_BACKUP_SUFFIX)
        _begin_immediate(db)
        repaired = 0
        for p in params:
            repaired += db.execute(f"UPDATE memories SET kind = 'profile' WHERE {match_sql}", p).rowcount
        db.execute(
            "INSERT INTO _kb_meta(key, value) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (_PROFILE_KIND_REPAIR_KEY, _now_iso()),
        )
        db.commit()
        if repaired:
            log.info(
                "[knowledge] 0125 profile-kind repair: re-kinded %d operator-profile memor%s fact → profile "
                "(backup: %s)",
                repaired,
                "y" if repaired == 1 else "ies",
                path.name + _PROFILE_KIND_REPAIR_BACKUP_SUFFIX,
            )
        return repaired
    except sqlite3.DatabaseError:
        try:
            db.rollback()
        except sqlite3.DatabaseError:
            pass
        log.exception("[knowledge] 0125 profile-kind repair failed at %s", path)
        return 0
    finally:
        db.close()


def _snapshot_profile_candidates(snapshot: Path) -> list[sqlite3.Row]:
    """The chunks in the pre-move snapshot that :func:`_memory_kind` now maps to
    ``profile`` but v0.231.0 mapped to ``fact``: rows the migration moves
    (``_MEMORY_PREDICATE``), in an operator-profile domain, with no typed kind.
    Opened read-only AND immutable: the snapshot is a frozen, checkpointed copy, and a
    plain ``mode=ro`` open of a WAL-mode file would still create ``-wal``/``-shm`` files
    beside it."""
    try:
        snap = sqlite3.connect(f"{snapshot.resolve().as_uri()}?mode=ro&immutable=1", uri=True)
    except sqlite3.DatabaseError:
        log.warning("[knowledge] 0125 profile-kind repair: cannot open snapshot %s", snapshot)
        return []
    snap.row_factory = sqlite3.Row
    try:
        if not _table_present(snap, "chunks"):
            return []
        doms = ", ".join("?" * len(_OPERATOR_PROFILE_DOMAINS))
        return snap.execute(
            "SELECT domain, source_type, content, heading, created_at, updated_at FROM chunks "
            f"WHERE {_MEMORY_PREDICATE} AND domain IN ({doms}) "
            "AND (memory_kind IS NULL OR memory_kind = 'legacy')",
            (*_MEMORY_PARAMS, *_OPERATOR_PROFILE_DOMAINS),
        ).fetchall()
    except sqlite3.DatabaseError:
        log.warning("[knowledge] 0125 profile-kind repair: unreadable snapshot %s", snapshot, exc_info=True)
        return []
    finally:
        snap.close()
