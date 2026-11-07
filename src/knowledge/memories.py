"""Core memory storage, delivery and operator review in private agent.db (ADR 0125)."""

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


def _normalize_subject(subject: str | None) -> str:
    """Case- and whitespace-folded subject key for supersession matching (ADR 0125
    P3-A). The row stores the subject AS GIVEN; this is only the comparison key, so
    ``"Comp Floor "`` and ``"comp floor"`` consolidate onto one subject. An empty or
    whitespace-only subject folds to ``""`` — never a supersession key."""
    return " ".join((subject or "").split()).lower()


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
        supersede: bool = True,
    ) -> int:
        """Persist a memory and its embedding atomically; operator rows default confirmed.

        When ``subject`` is set and ``supersede`` (the default), the new row retires every
        live row on the SAME subject, kind and scope (ADR 0125 P3-A) — see
        ``add_with_supersession`` for the exact rule. Returns the new id; callers that need
        the retired rows call ``add_with_supersession`` instead.
        """
        id, _ = self.add_with_supersession(
            content,
            kind=kind,
            provenance=provenance,
            subject=subject,
            heading=heading,
            delivery=delivery,
            review_state=review_state,
            scope=scope,
            expires_at=expires_at,
            source_session=source_session,
            thread_id=thread_id,
            legacy_domain=legacy_domain,
            supersede=supersede,
        )
        return id

    def add_with_supersession(
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
        supersede: bool = True,
    ) -> tuple[int, list[MemoryEntry]]:
        """Persist a memory and retire the rows it supersedes, atomically (ADR 0125 P3-A).

        Deterministic — there is no model at write time (ADR 0069's finding). When
        ``subject`` is set and ``supersede`` is true, within the SAME write transaction
        every live row (``superseded_by IS NULL``, ``invalidated_at IS NULL``,
        ``review_state != 'rejected'``) that shares the subject (case- and
        whitespace-normalised for matching, stored as given), the same ``kind`` and the
        same ``scope`` is stamped ``superseded_by`` = the new id and its ``updated_at``
        bumped. A row without a subject never supersedes and is never superseded.
        Pending replacements may retire only pending agent/extracted rows that are
        not always-on. Replacing operator-authored, confirmed or always-on rows
        requires a confirmed replacement. Rejected or expired writes retire nothing.
        A replacement must also expire no earlier than the row it retires; temporary
        saves preserve longer-lived memories so expiry cannot strand them.

        Returns ``(new_id, replaced)`` where ``replaced`` lists the retired rows as they
        were before the stamp (newest first).
        """
        review_state = (
            review_state if review_state is not None else ("confirmed" if provenance == "operator" else "pending")
        )
        vec = self._embed((heading + "\n" if heading else "") + content)
        now = _now_iso()
        expires_at = _normalize_expires_at(expires_at)
        norm_subject = _normalize_subject(subject)
        norm_scope = scope or None
        replaced: list[MemoryEntry] = []
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
                    expires_at,
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
            if supersede and norm_subject and review_state != "rejected" and (expires_at is None or expires_at > now):
                # Candidate live rows of the same kind; the subject (normalised) and scope
                # match is applied in Python so the fold matches ``_normalize_subject`` exactly.
                rows = db.execute(
                    "SELECT * FROM memories WHERE id != ? AND kind = ? AND subject IS NOT NULL "
                    "AND superseded_by IS NULL AND invalidated_at IS NULL AND review_state != 'rejected' "
                    "AND (expires_at IS NULL OR expires_at > ?) "
                    "ORDER BY created_at DESC, id DESC",
                    (id, kind, now),
                ).fetchall()
                for row in rows:
                    if _normalize_subject(row["subject"]) != norm_subject or (row["scope"] or None) != norm_scope:
                        continue
                    if expires_at is not None and (row["expires_at"] is None or expires_at < row["expires_at"]):
                        continue
                    if review_state != "confirmed" and (
                        row["provenance"] == "operator"
                        or row["review_state"] == "confirmed"
                        or row["delivery"] == "always"
                    ):
                        continue
                    db.execute(
                        "UPDATE memories SET superseded_by = ?, updated_at = ? WHERE id = ?",
                        (id, now, row["id"]),
                    )
                    replaced.append(MemoryEntry(**dict(row)))
        if delivery == "always":
            from knowledge.store import _publish_hot_write

            _publish_hot_write(f"memory:{id}", source_session, provenance, content)
        return id, replaced

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

    def live_on_subject(self, subject: str | None, *, exclude_id: int | None = None) -> list[MemoryEntry]:
        """Live rows on ``subject`` across every kind (ADR 0125 P3-A), newest first.

        The ``also live on this subject`` view ``memory_save`` shows after a write, so the
        agent can spot a wrong supersession. Live = eligible and unsuperseded (the default
        ``_filters``: not invalidated, not rejected, not expired, ``superseded_by IS NULL``).
        Subject matching uses the same case/whitespace fold as supersession; a null or
        empty subject matches nothing. ``exclude_id`` drops the row just written."""
        norm = _normalize_subject(subject)
        if not norm:
            return []
        where, params = self._filters()
        with closing(self._connect()) as db:
            rows = db.execute(
                f"SELECT m.* FROM memories m WHERE m.subject IS NOT NULL AND {where} "
                "ORDER BY m.created_at DESC, m.id DESC",
                params,
            ).fetchall()
        return [
            MemoryEntry(**dict(row))
            for row in rows
            if row["id"] != exclude_id and _normalize_subject(row["subject"]) == norm
        ]

    def inspect(self, limit: int = 500, *, delivery: str | None = None) -> list[MemoryEntry]:
        """Operator listing including rejected, expired and superseded audit rows.

        Filter delivery before applying the limit so unrelated rows cannot crowd
        always-on memories out of the inspector.
        """
        where = "invalidated_at IS NULL"
        params = []
        if delivery is not None:
            where += " AND delivery = ?"
            params.append(delivery)
        with closing(self._connect()) as db:
            return [
                MemoryEntry(**dict(row))
                for row in db.execute(
                    f"SELECT * FROM memories WHERE {where} ORDER BY created_at DESC, id DESC LIMIT ?",
                    [*params, limit],
                )
            ]

    def update(
        self,
        id: int,
        *,
        content: str | None = None,
        heading: str | None = None,
        subject: str | None = None,
        kind: str | None = None,
    ) -> bool:
        """Edit in place without losing lifecycle or provenance metadata.

        Only the fields passed change; the FTS/vector triggers re-index a
        heading/content change, and the embedding is recomputed exactly as ``add``
        does (heading + content) whenever either moves — the trigger drops the old
        vector on any heading/content edit, so leaving it would strand the row out
        of vector search. A subject/kind-only edit touches neither index."""
        fields = {
            col: value
            for col, value in (("content", content), ("heading", heading), ("subject", subject), ("kind", kind))
            if value is not None
        }
        if not fields:
            return False
        existing = self.get(id)
        if existing is None:
            return False
        vec = None
        if content is not None or heading is not None:
            eff_content = content if content is not None else existing.content
            eff_heading = heading if heading is not None else existing.heading
            vec = self._embed((eff_heading + "\n" if eff_heading else "") + eff_content)
        columns = ", ".join(f"{col} = ?" for col in fields)
        with self._write() as db:
            updated = (
                db.execute(
                    f"UPDATE memories SET {columns}, updated_at = ? WHERE id = ?",
                    (*fields.values(), _now_iso(), id),
                ).rowcount
                > 0
            )
            if updated and vec is not None:
                db.execute("INSERT INTO memory_vectors VALUES (?, ?)", (id, json.dumps(vec)))
        if updated and content is not None and existing.delivery == "always":
            from knowledge.store import _publish_hot_write

            _publish_hot_write(f"memory:{id}", existing.source_session, "operator", content)
        return updated

    @staticmethod
    def _detach_as_replacement(db, dead_id: int, now: str) -> None:
        """Splice a row out of every supersession chain it was the replacement in
        (ADR 0125 P3-A). Must run inside the caller's write transaction.

        When ``dead_id`` leaves the live set — the operator rejects it, or it is hard-
        deleted — the rows it had retired (``superseded_by = dead_id``) must not stay
        stranded pointing at a row no ``revert_supersession`` can restore: that would
        leave the subject with no live memory, the pre-PR behavior this card must not
        regress. Re-point each onto ``dead_id``'s OWN replacement (its ``superseded_by``,
        which is ``NULL`` when it was the live tip), so the oldest surviving row goes
        live again and a longer chain keeps exactly one live row. A single ``memory_save``
        can retire several rows at once, so this may re-home more than one. For a delete
        the caller runs this BEFORE the row is gone, so its ``superseded_by`` is still
        readable."""
        row = db.execute("SELECT superseded_by FROM memories WHERE id = ?", (dead_id,)).fetchone()
        heir = row["superseded_by"] if row is not None else None
        db.execute(
            "UPDATE memories SET superseded_by = ?, updated_at = ? WHERE superseded_by = ?",
            (heir, now, dead_id),
        )

    def set_review_state(self, id: int, state: str) -> bool:
        """Apply an operator review verdict, preserving the row for audit.

        Rejecting a row that had SUPERSEDED others (an agent/extracted ``memory_save``
        the operator overrules) re-homes those retired rows in the same transaction via
        ``_detach_as_replacement`` — so the subject keeps a live memory instead of being
        silently left empty (ADR 0125 P3-A)."""
        if state not in {"pending", "confirmed", "rejected"}:
            raise ValueError("invalid review state")
        now = _now_iso()
        with self._write() as db:
            updated = (
                db.execute(
                    "UPDATE memories SET review_state = ?, updated_at = ? WHERE id = ?",
                    (state, now, id),
                ).rowcount
                > 0
            )
            if updated and state == "rejected":
                self._detach_as_replacement(db, id, now)
            return updated

    def revert_supersession(self, old_id: int) -> bool:
        """Undo one supersession for audit (ADR 0125 P3-A, inspector revert).

        ``old_id`` was retired with ``superseded_by`` pointing at the row that replaced
        it; this clears that pointer (``old_id`` goes live again) and stamps the replacing
        row ``superseded_by = old_id`` — so both rows survive and exactly one is live.
        Returns False unless the old row is otherwise eligible and its direct
        replacement is still live and would not outlive the restored row. In particular,
        undo a chain from its live end first; never overwrite an intermediate row's
        audit link."""
        with self._write() as db:
            where, params = self._filters(include_superseded=True)
            row = db.execute(f"SELECT m.* FROM memories m WHERE m.id = ? AND {where}", [old_id, *params]).fetchone()
            if row is None or row["superseded_by"] is None:
                return False
            new_id = int(row["superseded_by"])
            where, params = self._filters()
            replacement = db.execute(
                f"SELECT m.* FROM memories m WHERE m.id = ? AND {where}", [new_id, *params]
            ).fetchone()
            if replacement is None:
                return False
            if row["expires_at"] is not None and (
                replacement["expires_at"] is None or row["expires_at"] < replacement["expires_at"]
            ):
                return False
            now = _now_iso()
            db.execute("UPDATE memories SET superseded_by = NULL, updated_at = ? WHERE id = ?", (now, old_id))
            db.execute("UPDATE memories SET superseded_by = ?, updated_at = ? WHERE id = ?", (old_id, now, new_id))
            return True

    def always_on_entries(self, max_chars: int = 6000) -> list[tuple[int, str]]:
        """Return exactly the rows rendered in the capped always-on block."""
        entries = []
        total = 0
        for entry in self.list(delivery="always"):
            piece = (f"[{entry.heading}] " if entry.heading else "") + entry.content
            size = len(piece) + bool(entries)
            if total + size > max_chars:
                break
            entries.append((entry.id, piece))
            total += size
        return entries

    def always_on(self, max_chars: int = 6000) -> str:
        """Format eligible always-on memories within a strict character cap."""
        return "\n".join(piece for _, piece in self.always_on_entries(max_chars))

    def delete(self, id: int) -> bool:
        """Hard-delete a row; triggers remove its FTS and vector entries atomically.

        If the deleted row had SUPERSEDED others, their ``superseded_by`` is re-homed
        onto its own replacement FIRST (``_detach_as_replacement``, same transaction), so
        deleting a replacement never strands the rows it retired with no live memory on
        the subject (ADR 0125 P3-A)."""
        now = _now_iso()
        with self._write() as db:
            self._detach_as_replacement(db, id, now)
            return db.execute("DELETE FROM memories WHERE id = ?", (id,)).rowcount > 0

    def stats(self) -> dict[str, int]:
        """Stored counts by kind, including audit rows, plus total."""
        with closing(self._connect()) as db:
            counts = {row[0]: row[1] for row in db.execute("SELECT kind, COUNT(*) FROM memories GROUP BY kind")}
        return {"profile": 0, "standing": 0, "fact": 0, "decision": 0, **counts, "total": sum(counts.values())}

    def count(self) -> int:
        """Number of stored memories, including audit rows."""
        return self.stats()["total"]
