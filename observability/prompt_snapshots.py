"""Per-call system-prompt snapshots (#2243).

The persistence behind the console's "View prompt": one row per model call
holding the EXACT system prompt that call received — the stable prefix (the
``build_system_prompt`` blob) hash-deduped into ``stable_blobs``, the volatile
per-call tail (``state["context"]``) inline — plus the call's real token usage.

Written best-effort from ``PromptCaptureMiddleware.wrap_model_call`` (a write
failure never breaks a turn); read by the operator console via
``GET /api/prompts/*`` (``operator_api/prompt_routes.py``). Instance-scoped
SQLite at ``instance_root/prompt-snapshots.db``, following the
``TelemetryStore`` conventions (WAL, busy_timeout, connection per call).

Retention is the ``MetricsStore`` in-write cap — age AND row-count trimmed
inside the same write transaction, no maintenance loop: snapshots are an
inspection substrate, not an archive. Stable blobs orphaned by a trim are
swept opportunistically in the same transaction.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

log = logging.getLogger(__name__)

# Defaults for the in-write retention cap. ``prompts.retention_days`` overrides
# the age cap; the row cap is a guardrail against pathological turn volume and
# stays constructor-only (the metrics-store precedent). <= 0 disables either.
RETENTION_DAYS = 30
MAX_CALLS = 5000


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


class PromptSnapshotStore:
    def __init__(
        self,
        db_path: str,
        *,
        retention_days: int = RETENTION_DAYS,
        max_calls: int = MAX_CALLS,
    ) -> None:
        self.path = str(db_path)
        self.retention_days = int(retention_days)
        self.max_calls = int(max_calls)
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
                CREATE TABLE IF NOT EXISTS stable_blobs (
                    hash       TEXT PRIMARY KEY,
                    text       TEXT NOT NULL,
                    created_at TEXT NOT NULL
                )
                """
            )
            db.execute(
                """
                CREATE TABLE IF NOT EXISTS calls (
                    id                    INTEGER PRIMARY KEY AUTOINCREMENT,
                    task_id               TEXT NOT NULL DEFAULT '',
                    session_id            TEXT NOT NULL DEFAULT '',
                    trace_id              TEXT NOT NULL DEFAULT '',
                    call_index            INTEGER NOT NULL DEFAULT 0,
                    ts                    TEXT NOT NULL,
                    stable_hash           TEXT NOT NULL DEFAULT '',
                    context_text          TEXT NOT NULL DEFAULT '',
                    model                 TEXT NOT NULL DEFAULT '',
                    input_tokens          INTEGER NOT NULL DEFAULT 0,
                    output_tokens         INTEGER NOT NULL DEFAULT 0,
                    cache_read_tokens     INTEGER NOT NULL DEFAULT 0,
                    cache_creation_tokens INTEGER NOT NULL DEFAULT 0
                )
                """
            )
            db.execute("CREATE INDEX IF NOT EXISTS ix_calls_task ON calls(task_id)")
            db.execute("CREATE INDEX IF NOT EXISTS ix_calls_session ON calls(session_id)")
            db.execute("CREATE INDEX IF NOT EXISTS ix_calls_ts ON calls(ts)")
            # Lightweight migrations for stores created before a column existed
            # (the TelemetryStore idiom): each ALTER fires once on an older DB and
            # no-ops after. `sections` (#2243 P2) hold JSON [{label, chars}] — on
            # stable_blobs for the deduped prefix, per call for the dynamic tail.
            for _table, _col in (
                ("stable_blobs", "sections"),
                ("calls", "context_sections"),
                # #2388 P3: subagent capture — a subagent call carries no task_id of its
                # own; it nests under the delegating tool-call id + its subagent type.
                ("calls", "parent_task_id"),
                ("calls", "subagent_type"),
            ):
                try:
                    db.execute(f"ALTER TABLE {_table} ADD COLUMN {_col} TEXT")
                except sqlite3.OperationalError:
                    pass  # column already present
            db.execute("CREATE INDEX IF NOT EXISTS ix_calls_parent ON calls(parent_task_id)")
            db.commit()
        finally:
            db.close()

    # ------------------------------------------------------------------ write

    def record(
        self,
        *,
        task_id: str = "",
        session_id: str = "",
        trace_id: str = "",
        stable_text: str = "",
        context_text: str = "",
        model: str = "",
        input_tokens: int = 0,
        output_tokens: int = 0,
        cache_read_tokens: int = 0,
        cache_creation_tokens: int = 0,
        stable_sections: list | None = None,
        context_sections: list | None = None,
        parent_task_id: str = "",
        subagent_type: str = "",
    ) -> None:
        """Append one snapshot row and trim to the retention caps in the same
        transaction. Best-effort — never raises (a capture write must not break
        the model call that triggered it).

        ``call_index`` is assigned here, inside the write transaction: the next
        index within ``task_id`` when one is present, else within the
        ``(session_id, trace_id)`` fallback key (non-A2A callers).
        """
        stable_hash = hashlib.sha256((stable_text or "").encode("utf-8")).hexdigest()
        try:
            db = self._connect()
        except sqlite3.DatabaseError:
            log.warning("[prompt-snapshots] connect failed at %s", self.path)
            return
        try:
            db.execute(
                "INSERT OR IGNORE INTO stable_blobs (hash, text, created_at, sections) VALUES (?, ?, ?, ?)",
                (
                    stable_hash,
                    stable_text or "",
                    _now_iso(),
                    json.dumps(stable_sections) if stable_sections else None,
                ),
            )
            if stable_sections:
                # A blob first captured without sections (pre-P2 rows, or a
                # graph rebuilt with segmentation) gains them in place — the
                # hash guarantees the labels describe this exact text.
                db.execute(
                    "UPDATE stable_blobs SET sections = ? WHERE hash = ? AND sections IS NULL",
                    (json.dumps(stable_sections), stable_hash),
                )
            if parent_task_id:
                # Subagent rows index within (parent turn, subagent type) — "Call N of
                # the researcher this turn", independent of the main loop's numbering.
                scope, params = (
                    "parent_task_id = ? AND subagent_type = ?",
                    (parent_task_id, subagent_type or ""),
                )
            elif task_id:
                scope, params = "task_id = ?", (task_id,)
            else:
                scope, params = "session_id = ? AND trace_id = ?", (session_id or "", trace_id or "")
            (next_index,) = db.execute(
                f"SELECT COALESCE(MAX(call_index) + 1, 0) FROM calls WHERE {scope}", params
            ).fetchone()
            db.execute(
                "INSERT INTO calls (task_id, session_id, trace_id, call_index, ts, stable_hash,"
                " context_text, model, input_tokens, output_tokens, cache_read_tokens,"
                " cache_creation_tokens, context_sections, parent_task_id, subagent_type)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    task_id or "",
                    session_id or "",
                    trace_id or "",
                    int(next_index),
                    _now_iso(),
                    stable_hash,
                    context_text or "",
                    model or "",
                    int(input_tokens),
                    int(output_tokens),
                    int(cache_read_tokens),
                    int(cache_creation_tokens),
                    json.dumps(context_sections) if context_sections else None,
                    parent_task_id or "",
                    subagent_type or "",
                ),
            )
            trimmed = 0
            if self.retention_days > 0:
                cutoff = (datetime.now(UTC) - timedelta(days=self.retention_days)).isoformat()
                trimmed += db.execute("DELETE FROM calls WHERE ts < ?", (cutoff,)).rowcount
            if self.max_calls > 0:
                # Keep the newest max_calls rows; LIMIT -1 OFFSET n = "everything
                # past the newest n" (the metrics-store trim shape).
                trimmed += db.execute(
                    "DELETE FROM calls WHERE id IN ( SELECT id FROM calls ORDER BY id DESC LIMIT -1 OFFSET ?)",
                    (self.max_calls,),
                ).rowcount
            if trimmed:
                self._sweep_orphan_blobs(db)
            db.commit()
        except sqlite3.DatabaseError as exc:
            log.warning("[prompt-snapshots] record failed: %s", exc)
        finally:
            db.close()

    @staticmethod
    def _sweep_orphan_blobs(db: sqlite3.Connection) -> None:
        """Drop stable blobs no call references anymore (same transaction)."""
        db.execute("DELETE FROM stable_blobs WHERE hash NOT IN (SELECT DISTINCT stable_hash FROM calls)")

    # ------------------------------------------------------------------- read

    _CALL_SELECT = (
        "SELECT c.task_id, c.session_id, c.trace_id, c.call_index, c.ts, c.model,"
        " c.context_text, c.input_tokens, c.output_tokens, c.cache_read_tokens,"
        " c.cache_creation_tokens, c.context_sections, c.parent_task_id, c.subagent_type,"
        " b.text AS stable_text, b.sections AS stable_sections"
        " FROM calls c LEFT JOIN stable_blobs b ON b.hash = c.stable_hash"
    )

    @staticmethod
    def _decode(row: sqlite3.Row) -> dict:
        """One joined row → dict with the JSON section columns decoded to
        Python lists (None stays None — 'captured unsegmented' is a state the
        reader distinguishes from 'no sections')."""
        d = dict(row)
        for col in ("stable_sections", "context_sections"):
            raw = d.get(col)
            if raw:
                try:
                    d[col] = json.loads(raw)
                except (json.JSONDecodeError, TypeError):
                    d[col] = None
        return d

    def calls_for_task(self, task_id: str) -> list[dict]:
        """Every captured call of one A2A turn, oldest first (call order), the
        stable blob resolved on read. Empty list when nothing was captured."""
        db = self._connect()
        try:
            rows = db.execute(
                f"{self._CALL_SELECT} WHERE c.task_id = ? ORDER BY c.call_index ASC, c.id ASC",
                (task_id,),
            ).fetchall()
        except sqlite3.DatabaseError as exc:
            log.warning("[prompt-snapshots] calls_for_task failed: %s", exc)
            return []
        finally:
            db.close()
        return [self._decode(r) for r in rows]

    def calls_for_parent(self, parent_task_id: str) -> list[dict]:
        """Every subagent call nested under one turn's delegating tool-call ids,
        grouped by type then call order (#2388 P3). Empty when none captured."""
        if not parent_task_id:
            return []
        db = self._connect()
        try:
            rows = db.execute(
                f"{self._CALL_SELECT} WHERE c.parent_task_id = ?"
                " ORDER BY c.subagent_type ASC, c.call_index ASC, c.id ASC",
                (parent_task_id,),
            ).fetchall()
        except sqlite3.DatabaseError as exc:
            log.warning("[prompt-snapshots] calls_for_parent failed: %s", exc)
            return []
        finally:
            db.close()
        return [self._decode(r) for r in rows]

    def previous_main_call(self, session_id: str, before_ts: str) -> dict | None:
        """The newest MAIN-LOOP call of ``session_id`` strictly older than
        ``before_ts`` — the previous turn's last call, the anchor the P3 diff
        compares against. Subagent rows are excluded (they aren't turns). None
        when there is no earlier capture (incognito gap, retention, first turn) —
        callers degrade to "no comparison available", never error."""
        if not session_id or not before_ts:
            return None
        db = self._connect()
        try:
            row = db.execute(
                f"{self._CALL_SELECT} WHERE c.session_id = ? AND c.ts < ?"
                " AND COALESCE(c.parent_task_id, '') = ''"
                " ORDER BY c.id DESC LIMIT 1",
                (session_id, before_ts),
            ).fetchone()
        except sqlite3.DatabaseError as exc:
            log.warning("[prompt-snapshots] previous_main_call failed: %s", exc)
            return None
        finally:
            db.close()
        return self._decode(row) if row is not None else None

    def last_for_session(self, session_id: str = "") -> dict | None:
        """The most recent captured call — of one session when ``session_id``
        is given, else across all sessions. None when nothing was captured."""
        where, params = "", []
        if session_id:
            where, params = "WHERE c.session_id = ?", [session_id]
        db = self._connect()
        try:
            row = db.execute(f"{self._CALL_SELECT} {where} ORDER BY c.id DESC LIMIT 1", params).fetchone()
        except sqlite3.DatabaseError as exc:
            log.warning("[prompt-snapshots] last_for_session failed: %s", exc)
            return None
        finally:
            db.close()
        return self._decode(row) if row is not None else None

    # ------------------------------------------------------------------ purge

    def purge_session(self, session_id: str) -> int:
        """Delete every snapshot of one chat session (the chat-delete hook —
        prompts never outlive their conversation). Returns rows deleted."""
        if not session_id:
            return 0
        db = self._connect()
        try:
            deleted = db.execute("DELETE FROM calls WHERE session_id = ?", (session_id,)).rowcount
            if deleted:
                self._sweep_orphan_blobs(db)
            db.commit()
            return int(deleted)
        except sqlite3.DatabaseError as exc:
            log.warning("[prompt-snapshots] purge_session failed: %s", exc)
            return 0
        finally:
            db.close()


# ---------------------------------------------------------------------------
# Default per-instance store (lazy singleton)
# ---------------------------------------------------------------------------

_default_store: PromptSnapshotStore | None = None


def prompt_snapshots() -> PromptSnapshotStore:
    """The per-instance snapshot store, created lazily on first use — NOT at
    import time (env identity is finalized after this module imports; mirrors
    ``injection_log()``). Shared by the middleware writer and the operator read
    routes so both see one DB. ``PROTOAGENT_PROMPT_SNAPSHOTS`` env overrides the
    path verbatim (the ``PROTOAGENT_INJECTION_LOG`` convention; the test suite
    points it at a temp DB so runs never append to a real instance store)."""
    global _default_store
    if _default_store is None:
        raw = os.environ.get("PROTOAGENT_PROMPT_SNAPSHOTS", "").strip()
        if raw:
            _default_store = PromptSnapshotStore(str(Path(raw).expanduser()))
        else:
            from infra.paths import instance_paths

            _default_store = PromptSnapshotStore(str(instance_paths().store("prompt-snapshots.db")))
    return _default_store


def reset_prompt_snapshots() -> None:
    """Drop the lazy singleton so the next ``prompt_snapshots()`` re-resolves
    its path from the environment (mirrors ``reset_injection_log``)."""
    global _default_store
    _default_store = None
