"""In-process tasks issue store (Sprint B).

A small SQLite-backed issue tracker the server owns — the agent's planning/task
surface and the console's Tasks panel both read/write it. Per-instance
(``instance_root/tasks/issues.db``) so several agents don't share one board. No
`br` CLI, no per-project `.tasks/` directory.

Issue shape (the fields the console + tools use):
  id, title, description, status, priority, issue_type, assignee,
  created_at, updated_at, closed_at, close_reason, links

``links`` (ADR 0129 Q1) ties a task to the work that delivers it — board card ids
(``bd-…``), scheduler job ids, a directive id, PR URLs — stored as a JSON array of
``{ref, kind, state, reason}``. :meth:`TaskStore.resolve_link` marks a ref terminal and
auto-closes the task once every linked item has finished, with an attributed
``close_reason`` ("bd-c8r5h merged (#5146)"). Unlinked tasks behave exactly as before.
"""

from __future__ import annotations

import json
import os
import sqlite3
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from infra.sqlite_locks import file_lock


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
# `waiting` (ADR 0129 Q1) = the next step is the operator's call — the agent parks the
# task there instead of leaving it in_progress for days. The legacy Tasks panel buckets
# statuses it doesn't know after the ordered ones; the Work view groups `waiting` under
# Needs you (apps/web/src/work/tone.ts `groupFor`/`fromTask`).
VALID_STATUSES = ("open", "in_progress", "blocked", "waiting", "deferred", "closed")
_VALID_TYPES = ("task", "bug", "feature", "chore", "epic")


def _link_kind(ref: str) -> str:
    """Classify a link ref by shape: board card (``bd-…``), directive (``dir-…``),
    PR URL, else a scheduler job id."""
    if ref.startswith("bd-"):
        return "card"
    if ref.startswith("dir-"):
        return "directive"
    if ref.startswith(("http://", "https://")):
        return "pr"
    return "job"


def parse_links(value: Any) -> list[dict]:
    """The decoded ``links`` entries of an issue row (or of a raw column value) —
    ``[{ref, kind, state, reason}, …]``; ``[]`` for no/invalid links. Tolerant on
    purpose: a hand-edited or legacy row must never break a read path."""
    raw = value.get("links", "") if isinstance(value, dict) else (value or "")
    if not raw:
        return []
    try:
        data = json.loads(raw)
    except (TypeError, ValueError):
        return []
    if not isinstance(data, list):
        return []
    return [e for e in data if isinstance(e, dict) and e.get("ref")]


def _close_reason(links: list[dict]) -> str:
    """The attributed close reason an all-links-done task carries —
    "bd-c8r5h merged (#5146); job-2 finished" (shared by :meth:`TaskStore.resolve_link`
    and the link-replacement path in :meth:`TaskStore.update`)."""
    return "; ".join(f"{e['ref']} {e.get('reason') or 'done'}" for e in links)


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
    """SQLite-backed issue tracker. A per-file lock guards each connection and
    queues stores on the same file in this process; write transactions
    serialize mutations across processes too."""

    def __init__(self, db_path: str | None = None):
        self.path = _resolve_db_path(db_path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = file_lock(self.path)
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
                    session_id   TEXT NOT NULL DEFAULT '',
                    links        TEXT NOT NULL DEFAULT ''
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
            # Links to the work that delivers a task (ADR 0129 Q1) — same guarded,
            # non-destructive upgrade as session_id above.
            if "links" not in cols:
                self._conn.execute("ALTER TABLE issues ADD COLUMN links TEXT NOT NULL DEFAULT ''")

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
        # The shared file lock queues same-process connections (see infra.sqlite_locks);
        # BEGIN IMMEDIATE + busy_timeout only arbitrates against other processes.
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

    @staticmethod
    def _norm_links(links: Any) -> str:
        """Serialize caller links (a list of refs, or of ``{ref, …}`` dicts) to the
        stored JSON. Deduplicates by ref; empty/None → '' (no links). A dict entry
        keeps a ``done`` state + reason (the restore/round-trip path); anything else
        starts ``open``."""
        if not links:
            return ""
        out: list[dict] = []
        seen: set[str] = set()
        for item in links:
            if isinstance(item, dict):
                ref = str(item.get("ref") or "").strip()
                entry = {
                    "ref": ref,
                    "kind": str(item.get("kind") or "").strip() or _link_kind(ref),
                    "state": "done" if item.get("state") == "done" else "open",
                    "reason": str(item.get("reason") or ""),
                }
            else:
                ref = str(item or "").strip()
                entry = {"ref": ref, "kind": _link_kind(ref), "state": "open", "reason": ""}
            if not entry["ref"] or entry["ref"] in seen:
                continue
            seen.add(entry["ref"])
            out.append(entry)
        return json.dumps(out) if out else ""

    @staticmethod
    def _merge_links(old_raw: Any, links: Any) -> list[dict]:
        """The replacement ``links`` normalized, with the resolution the task already
        holds carried forward: a ref the task had already marked ``done`` stays done
        (with its reason). ``done`` is one-way — the linked item's terminal event never
        fires again, so resetting it to ``open`` would leave the task unable to ever
        close itself (the ``task_update``/PATCH surfaces only carry plain refs)."""
        entries = json.loads(TaskStore._norm_links(links) or "[]")
        old = {e["ref"]: e for e in parse_links(old_raw)}
        for entry in entries:
            prev = old.get(entry["ref"])
            if prev is not None and prev.get("state") == "done" and entry["state"] != "done":
                entry["state"] = "done"
                entry["reason"] = str(prev.get("reason") or "")
        return entries

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
        links: Any = None,
    ) -> dict[str, Any]:
        title = (title or "").strip()
        if not title:
            raise ValueError("title is required")
        now = _now()
        with self._write():
            issue_id = self._next_id()
            self._conn.execute(
                "INSERT INTO issues (id, title, description, status, priority, issue_type, "
                "assignee, created_at, updated_at, session_id, links) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
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
                    self._norm_links(links),
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
        allowed = {"title", "description", "status", "priority", "issue_type", "assignee", "links"}
        # Accept `type` as an alias for issue_type (the console/tools use both).
        if "type" in fields and "issue_type" not in fields:
            fields["issue_type"] = fields.pop("type")
        # Links replace wholesale like every other field — [] clears, None keeps — but
        # are applied inside the transaction: the merge needs the current row so an
        # already-resolved ref the caller keeps stays ``done`` (see _merge_links).
        links = fields.pop("links", None)
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
        action = "updated"
        with self._write():
            row = self._row(issue_id)
            if row is None:
                raise KeyError(f"unknown issue {issue_id!r}")
            merged: list[dict] = []
            if links is not None:
                merged = self._merge_links(row["links"], links)
                sets.append("links = ?")
                vals.append(json.dumps(merged) if merged else "")
            now = _now()
            sets.append("updated_at = ?")
            vals.append(now)
            self._conn.execute(f"UPDATE issues SET {', '.join(sets)} WHERE id = ?", (*vals, issue_id))
            # The all-links-done invariant survives a replacement: when every ref the
            # caller kept has already finished, no further terminal event will ever
            # arrive — close now, attributed, exactly like resolve_link would have.
            if merged and all(e.get("state") == "done" for e in merged) and self._row(issue_id)["status"] != "closed":
                self._conn.execute(
                    "UPDATE issues SET status='closed', closed_at=?, close_reason=?, updated_at=? WHERE id=?",
                    (now, _close_reason(merged), now, issue_id),
                )
                action = "closed"
            result = dict(self._row(issue_id))
        _publish("task.changed", {"id": issue_id, "action": action})
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

    def resolve_link(self, ref: str, reason: str = "") -> list[dict[str, Any]]:
        """Mark linked item ``ref`` terminal on every open issue that links it (ADR 0129 Q1).

        ``reason`` is the attributed terminal fact ("merged (#5146)", "finished",
        "fulfilled: shipped"). An issue whose links are now ALL done auto-closes with
        ``close_reason`` built from its links ("bd-c8r5h merged (#5146)"; several join
        with "; "); one with links still open just records the resolution. Publishes
        ``task.changed`` (``closed`` / ``updated``) per affected issue, after commit —
        exactly like the other mutations. Returns the affected rows (often empty:
        events for refs nothing links are the common case and must stay cheap)."""
        ref = (ref or "").strip()
        if not ref:
            return []
        reason = (reason or "").strip()
        now = _now()
        affected: list[tuple[str, str]] = []  # (issue_id, action) in commit order
        results: list[dict[str, Any]] = []
        with self._write():
            rows = self._conn.execute(
                "SELECT id, links FROM issues WHERE status != 'closed' AND links != ''"
            ).fetchall()
            for row in rows:
                links = parse_links(row["links"])
                hit = False
                for entry in links:
                    if entry.get("ref") == ref and entry.get("state") != "done":
                        entry["state"] = "done"
                        entry["reason"] = reason
                        hit = True
                if not hit:
                    continue
                if all(e.get("state") == "done" for e in links):
                    self._conn.execute(
                        "UPDATE issues SET links=?, status='closed', closed_at=?, close_reason=?, "
                        "updated_at=? WHERE id=?",
                        (json.dumps(links), now, _close_reason(links), now, row["id"]),
                    )
                    affected.append((row["id"], "closed"))
                else:
                    self._conn.execute(
                        "UPDATE issues SET links=?, updated_at=? WHERE id=?",
                        (json.dumps(links), now, row["id"]),
                    )
                    affected.append((row["id"], "updated"))
            results = [dict(self._row(issue_id)) for issue_id, _action in affected]
        for issue_id, action in affected:
            _publish("task.changed", {"id": issue_id, "action": action})
        return results

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
