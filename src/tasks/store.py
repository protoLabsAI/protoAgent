"""In-process tasks issue store (Sprint B).

A small SQLite-backed issue tracker the server owns — the agent's planning/task
surface and the console's Tasks panel both read/write it. Per-instance
(``instance_root/tasks/issues.db``) so several agents don't share one board. No
`br` CLI, no per-project `.tasks/` directory.

Issue shape (the fields the console + tools use):
  id, title, description, status, priority, issue_type, assignee,
  created_at, updated_at, closed_at, close_reason
"""

from __future__ import annotations

import os
import sqlite3
import threading
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


def _publish(topic: str, data: dict) -> None:
    """Best-effort bus push so the console Tasks panel can invalidate live on a change
    instead of polling every 5s (#1310). No-op when the host hasn't wired a publisher
    (unit tests / standalone use); a bus hiccup must never break a task write."""
    try:
        from graph.plugins.host import HOST

        if HOST.publish:
            HOST.publish(topic, data)
    except Exception:  # noqa: BLE001
        pass


# Open lifecycle states (closed is terminal). Mirrors the console's
# issueStatusOrder; `tombstone` is intentionally absent (we hard-delete).
VALID_STATUSES = ("open", "in_progress", "blocked", "deferred", "closed")
_VALID_TYPES = ("task", "bug", "feature", "chore", "epic")


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _resolve_db_path(db_path: str | None) -> Path:
    """``TASKS_DB_PATH`` (or legacy ``BEADS_DB_PATH``) env → constructor arg
    (both verbatim) → the per-instance ``instance_root/tasks/issues.db`` store."""
    raw = os.environ.get("TASKS_DB_PATH") or os.environ.get("BEADS_DB_PATH") or db_path
    if raw:
        return Path(raw).expanduser()
    from infra.paths import instance_paths

    return instance_paths().store("tasks") / "issues.db"


class TaskStore:
    """SQLite-backed issue tracker. A lock guards each connection; write
    transactions serialize mutations across store instances too."""

    def __init__(self, db_path: str | None = None):
        self.path = _resolve_db_path(db_path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(str(self.path), check_same_thread=False)
        # busy_timeout FIRST: the WAL pragma itself takes a lock (#2428).
        self._conn.execute("PRAGMA busy_timeout=5000")  # wait (don't error) on lock contention
        self._conn.execute("PRAGMA journal_mode=WAL")  # concurrent reads during writes
        self._conn.row_factory = sqlite3.Row
        self._init_schema()

    def _init_schema(self) -> None:
        with self._write():
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS issues (
                    id           TEXT PRIMARY KEY,
                    title        TEXT NOT NULL,
                    description  TEXT NOT NULL DEFAULT '',
                    status       TEXT NOT NULL DEFAULT 'open',
                    priority     INTEGER NOT NULL DEFAULT 2,
                    issue_type   TEXT NOT NULL DEFAULT 'task',
                    assignee     TEXT NOT NULL DEFAULT '',
                    created_at   TEXT NOT NULL,
                    updated_at   TEXT NOT NULL,
                    closed_at    TEXT,
                    close_reason TEXT,
                    session_id   TEXT NOT NULL DEFAULT ''
                )
                """
            )
            # Attribution to the goal/session that motivated a task (ADR 0079). Guarded
            # migration for boards created before the column existed — ADD COLUMN is
            # non-destructive (existing rows get the '' default), so live prod boards
            # upgrade cleanly on first open.
            cols = {r["name"] for r in self._conn.execute("PRAGMA table_info(issues)").fetchall()}
            if "session_id" not in cols:
                self._conn.execute("ALTER TABLE issues ADD COLUMN session_id TEXT NOT NULL DEFAULT ''")

            # Persist the high-water mark independently of surviving tasks. Seed
            # existing boards from their rows without lowering it on restart.
            self._conn.execute(
                "CREATE TABLE IF NOT EXISTS task_sequence ("
                "singleton INTEGER PRIMARY KEY CHECK (singleton = 1), high_water INTEGER NOT NULL)"
            )
            self._conn.execute("INSERT OR IGNORE INTO task_sequence VALUES (1, 0)")
            for row in self._conn.execute("SELECT id FROM issues").fetchall():
                self._reserve_id(row["id"])

    @contextmanager
    def _write(self):
        # Serialize read/modify/write across connections too, and roll back failed
        # operations so neither partial writes nor allocated IDs leak into a retry.
        with self._lock, self._conn:
            self._conn.execute("BEGIN IMMEDIATE")
            yield

    # ── helpers ───────────────────────────────────────────────────────────────

    def _row(self, issue_id: str) -> sqlite3.Row | None:
        # Caller holds _lock until the cursor is consumed (including read-only
        # callers). check_same_thread=False does not serialize connection access.
        return self._conn.execute("SELECT * FROM issues WHERE id = ?", (issue_id,)).fetchone()

    def _reserve_id(self, issue_id: str) -> None:
        # Legacy bd- IDs share the numeric namespace with task- IDs.
        prefix, _, number = issue_id.partition("-")
        if prefix in {"task", "bd"} and number.isdigit():
            self._conn.execute(
                "UPDATE task_sequence SET high_water = MAX(high_water, ?) WHERE singleton = 1",
                (int(number),),
            )

    def _next_id(self) -> str:
        self._conn.execute("UPDATE task_sequence SET high_water = high_water + 1 WHERE singleton = 1")
        number = self._conn.execute("SELECT high_water FROM task_sequence WHERE singleton = 1").fetchone()[0]
        return f"task-{number}"

    @staticmethod
    def _norm_status(status: str | None) -> str:
        s = (status or "open").strip().lower()
        return s if s in VALID_STATUSES else "open"

    @staticmethod
    def _norm_type(t: str | None) -> str:
        t = (t or "task").strip().lower()
        return t if t in _VALID_TYPES else "task"

    # ── operations ──────────────────────────────────────────────────────────────

    def create(
        self,
        title: str,
        *,
        description: str = "",
        priority: int = 2,
        issue_type: str = "task",
        assignee: str = "",
        session_id: str = "",
    ) -> dict[str, Any]:
        title = (title or "").strip()
        if not title:
            raise ValueError("title is required")
        now = _now()
        with self._write():
            issue_id = self._next_id()
            self._conn.execute(
                "INSERT INTO issues (id, title, description, status, priority, issue_type, "
                "assignee, created_at, updated_at, session_id) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (
                    issue_id,
                    title,
                    description or "",
                    "open",
                    int(priority) if priority is not None else 2,
                    self._norm_type(issue_type),
                    assignee or "",
                    now,
                    now,
                    session_id or "",
                ),
            )
            result = dict(self._row(issue_id))
        _publish("task.changed", {"id": issue_id, "action": "created"})
        return result

    def list(self, *, include_closed: bool = True, session_id: str | None = None) -> list[dict[str, Any]]:
        where: list[str] = []
        params: list[Any] = []
        if not include_closed:
            where.append("status != 'closed'")
        if session_id is not None:
            # Scope to the goal/session that motivated the task (ADR 0079).
            where.append("session_id = ?")
            params.append(session_id)
        clause = f" WHERE {' AND '.join(where)}" if where else ""
        with self._lock:
            rows = self._conn.execute(f"SELECT * FROM issues{clause} ORDER BY created_at", params).fetchall()
            return [dict(r) for r in rows]

    def get(self, issue_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._row(issue_id)
            return dict(row) if row else None

    def update(self, issue_id: str, **fields: Any) -> dict[str, Any]:
        if not issue_id:
            raise ValueError("issue_id is required")
        allowed = {"title", "description", "status", "priority", "issue_type", "assignee"}
        # Accept `type` as an alias for issue_type (the console/tools use both).
        if "type" in fields and "issue_type" not in fields:
            fields["issue_type"] = fields.pop("type")
        sets: list[str] = []
        vals: list[Any] = []
        for key, value in fields.items():
            if key not in allowed or value is None:
                continue
            if key == "status":
                value = self._norm_status(str(value))
            elif key == "issue_type":
                value = self._norm_type(str(value))
            elif key == "priority":
                value = int(value)
            sets.append(f"{key} = ?")
            vals.append(value)
        with self._write():
            if self._row(issue_id) is None:
                raise KeyError(f"unknown issue {issue_id!r}")
            sets.append("updated_at = ?")
            vals.append(_now())
            self._conn.execute(f"UPDATE issues SET {', '.join(sets)} WHERE id = ?", (*vals, issue_id))
            result = dict(self._row(issue_id))
        _publish("task.changed", {"id": issue_id, "action": "updated"})
        return result

    def close(self, issue_id: str, reason: str | None = None) -> dict[str, Any]:
        if not issue_id:
            raise ValueError("issue_id is required")
        now = _now()
        with self._write():
            if self._row(issue_id) is None:
                raise KeyError(f"unknown issue {issue_id!r}")
            self._conn.execute(
                "UPDATE issues SET status='closed', closed_at=?, close_reason=?, updated_at=? WHERE id=?",
                (now, reason or "", now, issue_id),
            )
            result = dict(self._row(issue_id))
        _publish("task.changed", {"id": issue_id, "action": "closed"})
        return result

    def delete(self, issue_id: str) -> bool:
        return self.delete_with_snapshot(issue_id) is not None

    def delete_with_snapshot(self, issue_id: str) -> dict[str, Any] | None:
        """Atomically capture and delete a task, returning its full Undo snapshot.

        The write transaction prevents another connection from updating or
        replacing the row between the snapshot and deletion. Missing IDs return
        None and publish no event, just like :meth:`delete` returning False.
        """
        with self._write():
            row = self._row(issue_id)
            if row is None:
                return None
            snapshot = dict(row)
            self._conn.execute("DELETE FROM issues WHERE id = ?", (issue_id,))
        _publish("task.changed", {"id": issue_id, "action": "deleted"})
        return snapshot

    def restore(self, row: dict[str, Any]) -> dict[str, Any]:
        """Re-insert a previously deleted issue EXACTLY as it was — its original id and
        every stored column (status, priority, created/updated/closed timestamps,
        close_reason, session_id …) — and publish ``task.changed`` with
        ``action: "restored"`` so the console Tasks panel refreshes. Backs the server-side
        undo of a console task-delete (#4955 Q10).

        ``row`` is a snapshot taken by :meth:`delete_with_snapshot` (or a legacy
        caller's :meth:`get` before deletion); the insert covers
        whichever of the live table's columns the snapshot carries, so it survives a schema
        that gained a column after the snapshot was taken.

        Refuses (``ValueError``) if an issue already exists under that id: it was recreated
        between the delete and the undo, so restoring would clobber the newer row and its
        progress."""
        issue_id = row.get("id")
        if not issue_id:
            raise ValueError("restore: row has no id")
        with self._write():
            if self._row(issue_id) is not None:
                raise ValueError(f"issue {issue_id!r} already exists")
            cols = [r["name"] for r in self._conn.execute("PRAGMA table_info(issues)").fetchall()]
            present = [c for c in cols if c in row]
            self._conn.execute(
                f"INSERT INTO issues ({', '.join(present)}) VALUES ({', '.join('?' for _ in present)})",
                tuple(row[c] for c in present),
            )
            result = dict(self._row(issue_id))
            self._reserve_id(issue_id)
        _publish("task.changed", {"id": issue_id, "action": "restored"})
        return result
