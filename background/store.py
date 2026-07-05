"""BackgroundStore — a durable SQLite registry of background subagent jobs (ADR 0050).

A background job is a delegation the lead agent fired with ``run_in_background`` and
kept working past — it runs as its own detached A2A turn (see ``background.manager``).
This store is the registry that maps a job to the **chat session that spawned it**, so
the completion can be drained back into that session's next turn exactly once.

The mechanics mirror the inbox/scheduler stores: WAL sqlite, instance-scoped path, one
row per job. The ``notified`` flag is the linchpin — ``drain_pending`` returns completed
jobs and flips it atomically, so a completion is announced to the model exactly once.
"""

from __future__ import annotations

import sqlite3
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

STATUSES = ("running", "completed", "failed", "canceled")
_TERMINAL = ("completed", "failed", "canceled")


@dataclass
class BackgroundJob:
    id: str
    agent_name: str
    origin_session: str
    subagent_type: str
    description: str
    prompt: str
    status: str
    result: str
    notified: bool
    created_at: str
    completed_at: str | None
    a2a_task_id: str = ""  # the detached turn's A2A task id — the handle for stop_task
    # The spawning thread was incognito (ADR 0069 D3b → ADR 0070): the completion
    # must leave NO memory trail — no push-resume nudge, no knowledge-store indexing.
    # The report still lives here (jobs.db) and drains into the origin session normally.
    origin_incognito: bool = False
    # The spawning turn's id (#1766): every job a single fan-out turn spawns shares one
    # ``batch_id`` (task_batch's N specs, or several task(run_in_background=True) in one
    # turn), so the completions coalesce into ONE push-resume when the last member settles
    # instead of N drip-fed briefings. ``None`` for a lone / plugin spawn (a singleton).
    batch_id: str | None = None
    # Dismissed from the console Background-agents panel (#1808). A soft flag, NOT a delete:
    # the row (and its ``result``) is retained so the chat's report card can still open the
    # FULL report by id after the worker is dismissed — the report is the deliverable and
    # outlives the disposable worker (ADR 0070). ``list`` hides dismissed jobs from the panel;
    # ``get`` still returns them, so ``GET /api/background/{id}`` keeps working.
    dismissed: bool = False

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "agent_name": self.agent_name,
            "origin_session": self.origin_session,
            "subagent_type": self.subagent_type,
            "description": self.description,
            "status": self.status,
            "result": self.result,
            "notified": self.notified,
            "created_at": self.created_at,
            "completed_at": self.completed_at,
            "a2a_task_id": self.a2a_task_id,
            "origin_incognito": self.origin_incognito,
            "batch_id": self.batch_id,
            "dismissed": self.dismissed,
        }


def _row_to_job(row: sqlite3.Row) -> BackgroundJob:
    keys = row.keys()
    return BackgroundJob(
        id=row["id"],
        agent_name=row["agent_name"],
        origin_session=row["origin_session"],
        subagent_type=row["subagent_type"],
        description=row["description"],
        prompt=row["prompt"],
        status=row["status"],
        result=row["result"] or "",
        notified=bool(row["notified"]),
        created_at=row["created_at"],
        completed_at=row["completed_at"],
        a2a_task_id=(row["a2a_task_id"] if "a2a_task_id" in keys else "") or "",
        origin_incognito=bool(row["origin_incognito"] if "origin_incognito" in keys else 0),
        batch_id=(row["batch_id"] if "batch_id" in keys else None) or None,
        dismissed=bool(row["dismissed"] if "dismissed" in keys else 0),
    )


class BackgroundStore:
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
                CREATE TABLE IF NOT EXISTS background_jobs (
                    id             TEXT PRIMARY KEY,
                    agent_name     TEXT NOT NULL,
                    origin_session TEXT NOT NULL,
                    subagent_type  TEXT NOT NULL,
                    description    TEXT NOT NULL,
                    prompt         TEXT NOT NULL,
                    status         TEXT NOT NULL,
                    result         TEXT,
                    notified       INTEGER NOT NULL DEFAULT 0,
                    created_at     TEXT NOT NULL,
                    completed_at   TEXT,
                    a2a_task_id    TEXT,
                    origin_incognito INTEGER NOT NULL DEFAULT 0,
                    batch_id       TEXT,
                    dismissed      INTEGER NOT NULL DEFAULT 0
                )
                """
            )
            # Migrate a pre-ADR-0051 DB: add the a2a_task_id column if it's missing.
            cols = {r[1] for r in db.execute("PRAGMA table_info(background_jobs)").fetchall()}
            if "a2a_task_id" not in cols:
                db.execute("ALTER TABLE background_jobs ADD COLUMN a2a_task_id TEXT")
            # Migrate a pre-ADR-0070 DB: incognito propagation (existing rows default 0).
            if "origin_incognito" not in cols:
                db.execute("ALTER TABLE background_jobs ADD COLUMN origin_incognito INTEGER NOT NULL DEFAULT 0")
            # Migrate a pre-#1766 DB: fan-out batch key (existing rows are unbatched → NULL).
            if "batch_id" not in cols:
                db.execute("ALTER TABLE background_jobs ADD COLUMN batch_id TEXT")
            # Migrate a pre-#1808 DB: soft-dismiss flag (existing rows are undismissed → 0).
            if "dismissed" not in cols:
                db.execute("ALTER TABLE background_jobs ADD COLUMN dismissed INTEGER NOT NULL DEFAULT 0")
            db.execute(
                "CREATE INDEX IF NOT EXISTS ix_bg_session_pending ON background_jobs(origin_session, status, notified)"
            )
            db.execute("CREATE INDEX IF NOT EXISTS ix_bg_status ON background_jobs(status)")
            db.execute("CREATE INDEX IF NOT EXISTS ix_bg_batch ON background_jobs(batch_id)")
            db.commit()
        finally:
            db.close()

    # ── lifecycle ───────────────────────────────────────────────────────────

    def create(
        self,
        *,
        agent_name: str,
        origin_session: str,
        subagent_type: str,
        description: str,
        prompt: str,
        origin_incognito: bool = False,
        batch_id: str | None = None,
        now: datetime | None = None,
    ) -> str:
        """Insert a ``running`` job and return its opaque id (``bg-<uuid12>``).

        ``batch_id`` (#1766) tags a job as a member of a fan-out spawned by one turn, so
        the completions coalesce into ONE push-resume; ``None`` (the default) is a
        singleton spawn."""
        job_id = f"bg-{uuid.uuid4().hex[:12]}"
        created = (now or datetime.now(UTC)).isoformat()
        db = self._connect()
        try:
            db.execute(
                "INSERT INTO background_jobs "
                "(id, agent_name, origin_session, subagent_type, description, prompt, "
                " status, result, notified, created_at, completed_at, a2a_task_id, origin_incognito, batch_id) "
                "VALUES (?, ?, ?, ?, ?, ?, 'running', '', 0, ?, NULL, '', ?, ?)",
                (
                    job_id,
                    agent_name,
                    origin_session,
                    subagent_type,
                    description,
                    prompt,
                    created,
                    1 if origin_incognito else 0,
                    batch_id or None,
                ),
            )
            db.commit()
        finally:
            db.close()
        return job_id

    def set_a2a_task_id(self, job_id: str, a2a_task_id: str) -> None:
        """Record the detached turn's A2A task id (its stop/re-attach handle), set once
        when the background turn announces itself (ADR 0051). Only fills a blank slot so a
        late frame can't clobber it."""
        if not a2a_task_id:
            return
        db = self._connect()
        try:
            db.execute(
                "UPDATE background_jobs SET a2a_task_id = ? WHERE id = ? AND (a2a_task_id IS NULL OR a2a_task_id = '')",
                (a2a_task_id, job_id),
            )
            db.commit()
        finally:
            db.close()

    def mark_complete(
        self,
        job_id: str,
        status: str,
        result: str = "",
        *,
        now: datetime | None = None,
    ) -> bool:
        """Transition a job to a terminal state, idempotently.

        Returns ``True`` if this call performed the transition (the row was still
        ``running``), ``False`` if it was already terminal — so a redundant
        delivery-failure write can't clobber a real result.
        """
        if status not in _TERMINAL:
            raise ValueError(f"mark_complete status must be terminal, got {status!r}")
        completed = (now or datetime.now(UTC)).isoformat()
        db = self._connect()
        try:
            cur = db.execute(
                "UPDATE background_jobs SET status = ?, result = ?, completed_at = ? "
                "WHERE id = ? AND status = 'running'",
                (status, result or "", completed, job_id),
            )
            db.commit()
            return cur.rowcount > 0
        finally:
            db.close()

    def drain_pending(self, origin_session: str) -> list[BackgroundJob]:
        """Return completed/failed jobs for a session not yet announced, flipping
        ``notified`` in the same transaction so each is delivered exactly once."""
        db = self._connect()
        try:
            rows = db.execute(
                "SELECT * FROM background_jobs "
                "WHERE origin_session = ? AND status IN ('completed', 'failed', 'canceled') "
                "AND notified = 0 ORDER BY completed_at ASC",
                (origin_session,),
            ).fetchall()
            if rows:
                db.execute(
                    "UPDATE background_jobs SET notified = 1 WHERE origin_session = ? "
                    "AND status IN ('completed', 'failed', 'canceled') AND notified = 0",
                    (origin_session,),
                )
                db.commit()
            return [_row_to_job(r) for r in rows]
        finally:
            db.close()

    # ── fan-out batches (#1766) ───────────────────────────────────────────────

    def batch_size(self, batch_id: str) -> int:
        """Count every job in a fan-out batch. ``0`` for a null/unknown batch — the
        caller then treats the job as a singleton (the unchanged single-job path)."""
        if not batch_id:
            return 0
        db = self._connect()
        try:
            row = db.execute(
                "SELECT COUNT(*) AS n FROM background_jobs WHERE batch_id = ?", (batch_id,)
            ).fetchone()
            return int(row["n"]) if row else 0
        finally:
            db.close()

    def batch_outstanding(self, batch_id: str) -> int:
        """Count still-``running`` members of a batch — the "batch not yet fully settled"
        check. A queued-at-semaphore job still reads ``running`` (it IS accepted), so this
        stays >0 until every member reaches a terminal state."""
        if not batch_id:
            return 0
        db = self._connect()
        try:
            row = db.execute(
                "SELECT COUNT(*) AS n FROM background_jobs WHERE batch_id = ? AND status = 'running'",
                (batch_id,),
            ).fetchone()
            return int(row["n"]) if row else 0
        finally:
            db.close()

    def batch_status_counts(self, batch_id: str) -> dict[str, int]:
        """Per-status tallies for a batch, e.g. ``{'completed': 6, 'failed': 1}`` — the
        summary the join nudge quotes (a still-open batch also carries ``'running'``).
        Empty for a null/unknown batch."""
        if not batch_id:
            return {}
        db = self._connect()
        try:
            rows = db.execute(
                "SELECT status, COUNT(*) AS n FROM background_jobs WHERE batch_id = ? GROUP BY status",
                (batch_id,),
            ).fetchall()
            return {r["status"]: int(r["n"]) for r in rows}
        finally:
            db.close()

    def reconcile_interrupted(self) -> int:
        """Fail any job still ``running`` at startup — its detached turn died with
        the process. Returns the number of jobs reconciled. (Mirrors the A2A task
        store's restart reconciliation.)"""
        now = datetime.now(UTC).isoformat()
        db = self._connect()
        try:
            cur = db.execute(
                "UPDATE background_jobs SET status = 'failed', "
                "result = 'Interrupted — the background turn did not complete before a restart.', "
                "completed_at = ? WHERE status = 'running'",
                (now,),
            )
            db.commit()
            return cur.rowcount
        finally:
            db.close()

    # ── reads ───────────────────────────────────────────────────────────────

    def get(self, job_id: str) -> BackgroundJob | None:
        db = self._connect()
        try:
            row = db.execute("SELECT * FROM background_jobs WHERE id = ?", (job_id,)).fetchone()
            return _row_to_job(row) if row else None
        finally:
            db.close()

    def list(
        self,
        *,
        origin_session: str | None = None,
        status: str | None = None,
        limit: int = 100,
        include_dismissed: bool = False,
    ) -> list[BackgroundJob]:
        clauses, params = [], []
        if origin_session is not None:
            clauses.append("origin_session = ?")
            params.append(origin_session)
        if status is not None:
            clauses.append("status = ?")
            params.append(status)
        # Dismissed jobs (#1808) drop out of the panel listing but stay in the DB — their
        # report is still openable by id. Pass include_dismissed to see them anyway.
        if not include_dismissed:
            clauses.append("dismissed = 0")
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        params.append(int(limit))
        db = self._connect()
        try:
            rows = db.execute(
                f"SELECT * FROM background_jobs{where} ORDER BY created_at DESC LIMIT ?",
                params,
            ).fetchall()
            return [_row_to_job(r) for r in rows]
        finally:
            db.close()

    def dismiss(self, job_id: str) -> bool:
        """Dismiss a finished job from the panel (#1808) — a SOFT flag, not a delete. The row
        and its ``result`` are retained so the chat report card can still open the full report
        by id after the worker is gone (ADR 0070: the report outlives the disposable worker).
        A running job is left alone — cancel it first. Returns ``True`` if a row was dismissed
        (already-dismissed rows don't count, so a double-dismiss reports ``False``)."""
        db = self._connect()
        try:
            cur = db.execute(
                "UPDATE background_jobs SET dismissed = 1 WHERE id = ? AND status != 'running' AND dismissed = 0",
                (job_id,),
            )
            db.commit()
            return cur.rowcount > 0
        finally:
            db.close()

    def dismiss_finished(self, origin_session: str | None = None) -> int:
        """Dismiss all finished (non-running) jobs from the panel (#1808), optionally scoped to
        one originating session. Soft, like ``dismiss`` — rows are retained so their reports stay
        openable by id. Running jobs are kept. Returns the number newly dismissed."""
        clauses, params = ["status != 'running'", "dismissed = 0"], []
        if origin_session is not None:
            clauses.append("origin_session = ?")
            params.append(origin_session)
        db = self._connect()
        try:
            cur = db.execute(f"UPDATE background_jobs SET dismissed = 1 WHERE {' AND '.join(clauses)}", params)
            db.commit()
            return cur.rowcount
        finally:
            db.close()
