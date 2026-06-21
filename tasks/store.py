"""In-process tasks issue store (Sprint B).

A small SQLite-backed issue tracker the server owns — the agent's planning/task
surface and the console's Tasks panel both read/write it. Instance-scoped
(``paths.scope_leaf``) so several agents don't share one board. No `br` CLI, no
per-project `.tasks/` directory.

Issue shape (the fields the console + tools use):
  id, title, description, status, priority, issue_type, assignee,
  created_at, updated_at, closed_at, close_reason
"""

from __future__ import annotations

import os
import sqlite3
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from infra.paths import scope_leaf

DEFAULT_DB_PATH = "/sandbox/tasks/issues.db"

# Open lifecycle states (closed is terminal). Mirrors the console's
# issueStatusOrder; `tombstone` is intentionally absent (we hard-delete).
VALID_STATUSES = ("open", "in_progress", "blocked", "deferred", "closed")
_VALID_TYPES = ("task", "bug", "feature", "chore", "epic")


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _resolve_db_path(db_path: str | None) -> Path:
    """``TASKS_DB_PATH`` (or legacy ``BEADS_DB_PATH``) env → constructor arg →
    default. Falls back from a non-writable ``/sandbox`` to ``~/.protoagent`` for
    local dev, then instance-scoped — same shape as the knowledge store."""
    raw = os.environ.get("TASKS_DB_PATH") or os.environ.get("BEADS_DB_PATH") or db_path or DEFAULT_DB_PATH
    p = Path(raw).expanduser()
    if str(p).startswith("/sandbox") and not Path("/sandbox").is_dir():
        p = Path.home() / ".protoagent" / "tasks" / "issues.db"
    return scope_leaf(p)


class TaskStore:
    """SQLite-backed issue tracker. Thread-safe via a single lock (the server
    runs one process; contention is low)."""

    def __init__(self, db_path: str | None = None):
        self.path = _resolve_db_path(db_path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(str(self.path), check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL")  # concurrent reads during writes
        self._conn.execute("PRAGMA busy_timeout=5000")  # wait (don't error) on lock contention
        self._conn.row_factory = sqlite3.Row
        self._init_schema()

    def _init_schema(self) -> None:
        with self._lock:
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
                    close_reason TEXT
                )
                """
            )
            self._conn.commit()

    # ── helpers ───────────────────────────────────────────────────────────────

    def _row(self, issue_id: str) -> sqlite3.Row | None:
        return self._conn.execute("SELECT * FROM issues WHERE id = ?", (issue_id,)).fetchone()

    def _next_id(self) -> str:
        # New ids use the `task-` prefix; legacy `bd-` ids (pre beads→tasks rename)
        # are still read so numbering stays monotonic on an existing board.
        rows = self._conn.execute(
            "SELECT id FROM issues WHERE id LIKE 'task-%' OR id LIKE 'bd-%'"
        ).fetchall()
        nums = [int(n) for r in rows if (n := r["id"].split("-", 1)[-1]).isdigit()]
        return f"task-{(max(nums) + 1) if nums else 1}"

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
    ) -> dict[str, Any]:
        title = (title or "").strip()
        if not title:
            raise ValueError("title is required")
        now = _now()
        with self._lock:
            issue_id = self._next_id()
            self._conn.execute(
                "INSERT INTO issues (id, title, description, status, priority, issue_type, "
                "assignee, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?)",
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
                ),
            )
            self._conn.commit()
            return dict(self._row(issue_id))

    def list(self, *, include_closed: bool = True) -> list[dict[str, Any]]:
        if include_closed:
            rows = self._conn.execute("SELECT * FROM issues ORDER BY created_at").fetchall()
        else:
            rows = self._conn.execute("SELECT * FROM issues WHERE status != 'closed' ORDER BY created_at").fetchall()
        return [dict(r) for r in rows]

    def get(self, issue_id: str) -> dict[str, Any] | None:
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
        with self._lock:
            if self._row(issue_id) is None:
                raise KeyError(f"unknown issue {issue_id!r}")
            sets.append("updated_at = ?")
            vals.append(_now())
            self._conn.execute(f"UPDATE issues SET {', '.join(sets)} WHERE id = ?", (*vals, issue_id))
            self._conn.commit()
            return dict(self._row(issue_id))

    def close(self, issue_id: str, reason: str | None = None) -> dict[str, Any]:
        if not issue_id:
            raise ValueError("issue_id is required")
        now = _now()
        with self._lock:
            if self._row(issue_id) is None:
                raise KeyError(f"unknown issue {issue_id!r}")
            self._conn.execute(
                "UPDATE issues SET status='closed', closed_at=?, close_reason=?, updated_at=? WHERE id=?",
                (now, reason or "", now, issue_id),
            )
            self._conn.commit()
            return dict(self._row(issue_id))

    def delete(self, issue_id: str) -> bool:
        with self._lock:
            cur = self._conn.execute("DELETE FROM issues WHERE id = ?", (issue_id,))
            self._conn.commit()
            return cur.rowcount > 0
