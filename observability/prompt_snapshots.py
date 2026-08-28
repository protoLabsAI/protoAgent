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
swept opportunistically in the same transaction. BOTH caps are operator
config (``prompts.retention_days`` / ``prompts.max_calls``) because on a busy
agent the row cap binds first and an age-only knob is inert (#3019);
``retention_stats`` reports which of the two is actually governing.
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

# Defaults for the in-write retention cap: ``prompts.retention_days`` overrides
# the age cap, ``prompts.max_calls`` the row cap. Both are operator-reachable
# because they are NOT independent — whichever bites first is the real window,
# and at even moderate turn volume that is the row cap, which made the age knob
# look broken (#3019). <= 0 disables either.
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
        # busy_timeout FIRST: the WAL pragma itself takes a lock, so setting the
        # timeout after it leaves an unguarded window that raises "database is
        # locked" under contention (#2428 — wide enough to flake on Windows).
        db.execute("PRAGMA busy_timeout=5000")  # wait (don't error) on lock contention
        db.execute("PRAGMA journal_mode=WAL")  # concurrent reads during writes
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
            # stable_hash is the orphan sweep's lookup column: once the store sits at
            # the row cap that sweep runs on EVERY write, and unindexed it re-scanned
            # the whole calls table each time (#3019). IF NOT EXISTS means an existing
            # store picks the index up on its next open, like the ALTERs below.
            db.execute("CREATE INDEX IF NOT EXISTS ix_calls_stable_hash ON calls(stable_hash)")
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
                # #2527: the wire text when it DIFFERED from the composed prompt.
                # NULL = faithful delivery (or pre-observer row); '' = the call
                # carried NO system text (the #2519 failure class, made visible).
                ("calls", "wire_text"),
                # #3191 (ADR 0108 D2): the projected context (memory, skills,
                # working state) delivered ephemerally via wrap_model_call.
                ("calls", "projected_context"),
                ("calls", "projected_sections"),
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
        wire_text: str | None = None,
        projected_context: str | None = None,
        projected_sections: list | None = None,
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
                " cache_creation_tokens, context_sections, parent_task_id, subagent_type, wire_text,"
                " projected_context, projected_sections)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
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
                    wire_text,  # None = faithful; '' = nothing delivered (#2527)
                    projected_context,  # None = no projection (#3191)
                    json.dumps(projected_sections) if projected_sections else None,
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
        "SELECT c.task_id, c.session_id, c.trace_id, c.call_index, c.ts, c.model, c.wire_text,"
        " c.context_text, c.input_tokens, c.output_tokens, c.cache_read_tokens,"
        " c.cache_creation_tokens, c.context_sections, c.parent_task_id, c.subagent_type,"
        " c.projected_context, c.projected_sections,"
        " b.text AS stable_text, b.sections AS stable_sections"
        " FROM calls c LEFT JOIN stable_blobs b ON b.hash = c.stable_hash"
    )

    @staticmethod
    def _decode(row: sqlite3.Row) -> dict:
        """One joined row → dict with the JSON section columns decoded to
        Python lists (None stays None — 'captured unsegmented' is a state the
        reader distinguishes from 'no sections')."""
        d = dict(row)
        for col in ("stable_sections", "context_sections", "projected_sections"):
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

    def retention_stats(self, *, retention_days: int | None = None, max_calls: int | None = None) -> dict:
        """Which cap is ACTUALLY governing the retained window right now (#3019).

        Both caps trim on every write, so a configured ``retention_days`` is only
        the real window while the row count stays below ``max_calls``. On a busy
        agent it does not: the row cap evicts rows the age cap would have kept,
        and the operator had no way to see that short of opening the SQLite file
        (the live PM sat at 4,968 rows = ~3 days against a configured 30).

        ``retention_days``/``max_calls`` override the store's own attributes for
        the report. The reader NEEDS that: those attributes are set by the
        *writer* (``PromptCaptureMiddleware._store``) on each capture, so a
        process that has not captured yet — a console hitting ``/prompt`` on an
        old session right after a restart — still holds the construction
        defaults. Reporting them would answer with 30/5000 no matter what the
        operator configured, which for a knob-is-inert diagnostic is worse than
        not answering: it would keep crying ``max_calls`` at someone who had
        already raised it. The route passes the live config; ``None`` means
        "whatever this store is set to" (the writer's own view).

        ``binding_cap`` names the cap that is ending the window *right now*, so
        it is only ever a cap actually at its limit. ``"max_calls"`` = the store
        sits at the row cap and what survived is younger than ``retention_days``
        — the state where the age knob is inert. ``"retention_days"`` = the
        oldest row has reached the age cutoff, so age is what ends the window.
        ``"none"`` = nothing has been evicted yet: an empty store, a store still
        filling under caps neither of which it has reached, or both caps
        disabled. A young store under generous caps is deliberately ``"none"``
        and not ``"retention_days"``: naming a cap nothing has hit yet reads as a
        diagnosis, and this field exists to be read as one. ``effective_days`` is
        how far back the store actually reaches, so "my 30 days is really 3"
        reads off the payload whatever the verdict.
        """
        days = self.retention_days if retention_days is None else int(retention_days)
        rows = self.max_calls if max_calls is None else int(max_calls)
        stats: dict = {
            "retention_days": days,
            "max_calls": rows,
            "calls": 0,
            "oldest_ts": "",
            "newest_ts": "",
            "effective_days": None,
            "binding_cap": "none",
        }
        try:
            db = self._connect()
        except sqlite3.DatabaseError:
            # A diagnostic must never cost its caller the thing it diagnoses:
            # this rides GET /api/prompts/last, and a locked/unreadable DB has
            # to degrade to honest zeros rather than 500 the prompt read.
            log.warning("[prompt-snapshots] retention_stats connect failed at %s", self.path)
            return stats
        try:
            calls, oldest, newest = db.execute("SELECT COUNT(*), MIN(ts), MAX(ts) FROM calls").fetchone()
        except sqlite3.DatabaseError as exc:
            log.warning("[prompt-snapshots] retention_stats failed: %s", exc)
            return stats
        finally:
            db.close()
        stats["calls"] = int(calls or 0)
        stats["oldest_ts"] = oldest or ""
        stats["newest_ts"] = newest or ""
        if not stats["calls"]:
            return stats
        age_days = None
        try:
            age = datetime.now(UTC) - datetime.fromisoformat(stats["oldest_ts"])
            age_days = age.total_seconds() / 86400.0
        except (TypeError, ValueError):
            pass  # an unparseable stamp costs the span, not the rest of the answer
        stats["effective_days"] = None if age_days is None else round(age_days, 2)
        at_row_cap = rows > 0 and stats["calls"] >= rows
        # The age cap can only be ENDING a window it has actually reached: the
        # trim drops rows older than the cutoff on every write, so while the
        # oldest row is younger than retention_days the age cap has evicted
        # nothing and naming it would invent a diagnosis. The comparison is on
        # the unrounded age — `effective_days` is a display figure. (A quiet
        # instance drifts past its own cutoff between writes, which is why this
        # is `>=` on an age that can exceed the cap rather than equality.)
        age_cap_reached = days > 0 and age_days is not None and age_days >= days
        # Sitting AT the row cap is observed eviction — every write past this
        # point drops a row. The span only refines whether the age cap would
        # have kept it, so a span we could not compute must not talk us out of
        # the alarm; it degrades toward reporting the cap we can see biting.
        if at_row_cap and not age_cap_reached:
            stats["binding_cap"] = "max_calls"
        elif age_cap_reached:
            stats["binding_cap"] = "retention_days"
        return stats

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
