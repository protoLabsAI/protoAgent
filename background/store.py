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
    )


class BackgroundStore:
    def __init__(self, db_path: str) -> None:
        self.path = str(db_path)
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.path)
        db.execute("PRAGMA journal_mode=WAL")   # concurrent reads during writes
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
                    a2a_task_id    TEXT
                )
                """
            )
            # Migrate a pre-ADR-0051 DB: add the a2a_task_id column if it's missing.
            cols = {r[1] for r in db.execute("PRAGMA table_info(background_jobs)").fetchall()}
            if "a2a_task_id" not in cols:
                db.execute("ALTER TABLE background_jobs ADD COLUMN a2a_task_id TEXT")
            db.execute(
                "CREATE INDEX IF NOT EXISTS ix_bg_session_pending "
                "ON background_jobs(origin_session, status, notified)"
            )
            db.execute("CREATE INDEX IF NOT EXISTS ix_bg_status ON background_jobs(status)")
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
        now: datetime | None = None,
    ) -> str:
        """Insert a ``running`` job and return its opaque id (``bg-<uuid12>``)."""
        job_id = f"bg-{uuid.uuid4().hex[:12]}"
        created = (now or datetime.now(UTC)).isoformat()
        db = self._connect()
        try:
            db.execute(
                "INSERT INTO background_jobs "
                "(id, agent_name, origin_session, subagent_type, description, prompt, "
                " status, result, notified, created_at, completed_at, a2a_task_id) "
                "VALUES (?, ?, ?, ?, ?, ?, 'running', '', 0, ?, NULL, '')",
                (job_id, agent_name, origin_session, subagent_type, description, prompt, created),
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
                "UPDATE background_jobs SET a2a_task_id = ? "
                "WHERE id = ? AND (a2a_task_id IS NULL OR a2a_task_id = '')",
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
            row = db.execute(
                "SELECT * FROM background_jobs WHERE id = ?", (job_id,)
            ).fetchone()
            return _row_to_job(row) if row else None
        finally:
            db.close()

    def list(
        self,
        *,
        origin_session: str | None = None,
        status: str | None = None,
        limit: int = 100,
    ) -> list[BackgroundJob]:
        clauses, params = [], []
        if origin_session is not None:
            clauses.append("origin_session = ?")
            params.append(origin_session)
        if status is not None:
            clauses.append("status = ?")
            params.append(status)
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
