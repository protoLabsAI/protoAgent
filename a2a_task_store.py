"""Durable persistence for A2A task records (A2A spec / ADR 0003).

Pairs with the in-memory ``A2ATaskStore``: task records are written through to
SQLite on create and on every terminal transition, so ``tasks/get`` /
``tasks/resubscribe`` answer with the final state + artifacts even after the
in-memory copy is evicted (1h) or the process restarts (within the 24h TTL).

The actual *work* (the LangGraph background runner) does not survive a restart,
so on boot any task still in a non-terminal state is marked ``failed`` — it
would otherwise hang forever. The in-memory store lazy-loads a row from here on
a cache miss.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

log = logging.getLogger(__name__)

_DEFAULT_TTL_S = 24 * 60 * 60  # 24h, matching the push-config store


class A2ATaskPersistence:
    def __init__(self, db_path: str, *, ttl_s: int = _DEFAULT_TTL_S) -> None:
        self.path = str(db_path)
        self._ttl_s = ttl_s
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.path)
        db.row_factory = sqlite3.Row
        return db

    def _init_db(self) -> None:
        db = self._connect()
        try:
            db.execute(
                """
                CREATE TABLE IF NOT EXISTS tasks (
                    task_id    TEXT PRIMARY KEY,
                    state      TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    data       TEXT NOT NULL
                )
                """
            )
            db.execute("CREATE INDEX IF NOT EXISTS ix_tasks_state ON tasks(state)")
            db.commit()
        finally:
            db.close()

    def save(self, row: dict) -> None:
        """Upsert a serialized task record (``row`` must carry id/state/updated_at)."""
        task_id = row.get("id")
        if not task_id:
            return
        db = self._connect()
        try:
            db.execute(
                "INSERT INTO tasks (task_id, state, updated_at, data) VALUES (?, ?, ?, ?) "
                "ON CONFLICT(task_id) DO UPDATE SET state=excluded.state, "
                "updated_at=excluded.updated_at, data=excluded.data",
                (task_id, row.get("state", ""), row.get("updated_at", ""), json.dumps(row)),
            )
            db.commit()
        finally:
            db.close()

    def get(self, task_id: str) -> dict | None:
        db = self._connect()
        try:
            r = db.execute("SELECT data FROM tasks WHERE task_id = ?", (task_id,)).fetchone()
        finally:
            db.close()
        if r is None:
            return None
        try:
            return json.loads(r["data"])
        except (ValueError, TypeError):
            return None

    def delete(self, task_id: str) -> None:
        db = self._connect()
        try:
            db.execute("DELETE FROM tasks WHERE task_id = ?", (task_id,))
            db.commit()
        finally:
            db.close()

    def sweep_expired(self, *, now: datetime | None = None) -> int:
        now = now or datetime.now(UTC)
        cutoff = (now - timedelta(seconds=self._ttl_s)).isoformat()
        db = self._connect()
        try:
            cur = db.execute("DELETE FROM tasks WHERE updated_at < ?", (cutoff,))
            db.commit()
            return cur.rowcount
        finally:
            db.close()

    def fail_interrupted(self, terminal_states: tuple[str, ...], *, error: str, now: datetime | None = None) -> int:
        """Mark any persisted non-terminal task as failed — its runner did not
        survive the restart, so it would otherwise hang. Returns the count."""
        now = now or datetime.now(UTC)
        placeholders = ",".join("?" for _ in terminal_states)
        db = self._connect()
        try:
            rows = db.execute(
                f"SELECT task_id, data FROM tasks WHERE state NOT IN ({placeholders})",
                terminal_states,
            ).fetchall()
            for row in rows:
                try:
                    data = json.loads(row["data"])
                except (ValueError, TypeError):
                    data = {"id": row["task_id"]}
                data["state"] = "failed"
                data["error_message"] = error
                data["updated_at"] = now.isoformat()
                db.execute(
                    "UPDATE tasks SET state='failed', updated_at=?, data=? WHERE task_id=?",
                    (data["updated_at"], json.dumps(data), row["task_id"]),
                )
            db.commit()
            return len(rows)
        finally:
            db.close()
