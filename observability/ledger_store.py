"""Durable record of delegation edges — who handed what work to whom.

The gap this fills. Three systems already describe the fleet and none of them records
the edge that carried the work:

- ``orgchart`` draws the **capability** graph (who *can* delegate to whom). Live, crawled,
  TTL-cached, nothing persisted.
- ``observability/telemetry_store`` records work **volume** per turn leg — but it has no
  actor column and no edge. Attribution is only "which instance's DB the row landed in".
- ``graph/delegations`` is an in-memory registry of cancel handles for in-flight ``task``
  calls, discarded on exit.

So an operator can see how much an agent spent and who it *could* have delegated to, but
never what actually flowed along which edge. That is what this table is.

Written through the single writer ``graph/ledger.py::record_delegation`` — the same shape
as ``server/turn_telemetry.py::record_turn``, and for the same reason: a new dispatch
surface routes through it or the edge is unrecorded. That seam exists because CLI coding
agents were invisible to turn telemetry for months (#3015) precisely for want of it.

Rows join to ``turns`` on ``task_id`` / ``session_id``, which is what lets "what did this
delegation cost" be answered without duplicating the token accounting.

Instance-scoped via the path the host resolves (ADR 0004). Best-effort: a write failure
never breaks a dispatch. No TTL — history is the point; ``prune`` exists for hosts that
want to cap retention.
"""

from __future__ import annotations

import logging
import sqlite3
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

log = logging.getLogger(__name__)

#: Bounds on free text, so one runaway prompt can't dominate the table. The ledger stores
#: enough to identify a delegation, not to replay it — the prompt itself already lives in
#: the background store, the trajectory, and the chat thread.
_WHAT_LIMIT = 500
_ERROR_LIMIT = 500

#: The delegation target kinds. ``subagent`` is in-process (ADR 0002); the rest mirror the
#: delegate registry's types (ADR 0025).
KINDS = ("subagent", "a2a", "acp", "openai")

#: Terminal outcomes. ``cancelled`` is deliberately distinct from ``failed``: an operator
#: stopping a turn says nothing about the delegate, and collapsing the two would put a red
#: mark on a healthy coder every time someone hit stop (the same rule the delegates
#: plugin's last-dispatch tracker already follows).
OUTCOMES = ("ok", "failed", "cancelled")

_COLUMNS = (
    "at",
    "from_agent",
    "to_kind",
    "to_name",
    "to_instance",
    "what",
    "session_id",
    "parent_task_id",
    "task_id",
    "outcome",
    "error",
    "duration_ms",
    "cost_usd",
    "origin",
)


def _clip(value: object, limit: int) -> str:
    text = "" if value is None else str(value)
    return text if len(text) <= limit else text[: limit - 1] + "…"


class LedgerStore:
    def __init__(self, db_path: str) -> None:
        self.path = str(db_path)
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.path)
        # busy_timeout FIRST: the WAL pragma itself takes a lock, so setting the timeout
        # after it leaves an unguarded window that raises "database is locked" under
        # contention (the same ordering trap fixed in the telemetry store, #2428).
        db.execute("PRAGMA busy_timeout=5000")
        db.execute("PRAGMA journal_mode=WAL")
        db.row_factory = sqlite3.Row
        return db

    def _init_db(self) -> None:
        db = self._connect()
        try:
            db.execute(
                """
                CREATE TABLE IF NOT EXISTS delegations (
                    edge_id        INTEGER PRIMARY KEY AUTOINCREMENT,
                    at             TEXT,
                    from_agent     TEXT,
                    to_kind        TEXT,
                    to_name        TEXT,
                    to_instance    TEXT,
                    what           TEXT,
                    session_id     TEXT,
                    parent_task_id TEXT,
                    task_id        TEXT,
                    outcome        TEXT,
                    error          TEXT,
                    duration_ms    INTEGER DEFAULT 0,
                    cost_usd       REAL,
                    origin         TEXT
                )
                """
            )
            # The identity is a surrogate on purpose. `task_id` is NOT unique — a HITL
            # park/resume shares one across legs, and a fan-out shares a parent — so
            # keying on it would silently overwrite edges, which is exactly the defect
            # the telemetry store had to migrate away from (#3001).
            db.execute("CREATE INDEX IF NOT EXISTS ix_deleg_at ON delegations(at)")
            db.execute("CREATE INDEX IF NOT EXISTS ix_deleg_task ON delegations(task_id)")
            db.execute("CREATE INDEX IF NOT EXISTS ix_deleg_session ON delegations(session_id)")
            db.execute("CREATE INDEX IF NOT EXISTS ix_deleg_to ON delegations(to_kind, to_name)")
            db.commit()
        finally:
            db.close()

    def record(
        self,
        *,
        from_agent: str,
        to_kind: str,
        to_name: str,
        to_instance: str = "",
        what: str = "",
        session_id: str = "",
        parent_task_id: str = "",
        task_id: str = "",
        outcome: str = "ok",
        error: str = "",
        duration_ms: int = 0,
        cost_usd: float | None = None,
        origin: str = "",
        at: str | None = None,
    ) -> int | None:
        """Append one delegation edge. Returns its ``edge_id``, or None on failure.

        ``cost_usd`` is deliberately nullable. Only some paths know a real number — an
        a2a peer transmits ``cost-v1``, an acp coder exposes ``last_usage`` — and storing
        a confident ``0`` where the cost is merely *unknown* would make an expensive
        coder look free and silently understate every rollup built on this column.
        """
        row = {
            "at": at or datetime.now(UTC).isoformat(),
            "from_agent": str(from_agent or ""),
            "to_kind": str(to_kind or ""),
            "to_name": str(to_name or ""),
            "to_instance": str(to_instance or ""),
            "what": _clip(what, _WHAT_LIMIT),
            "session_id": str(session_id or ""),
            "parent_task_id": str(parent_task_id or ""),
            "task_id": str(task_id or ""),
            "outcome": str(outcome or "ok"),
            "error": _clip(error, _ERROR_LIMIT),
            "duration_ms": int(duration_ms or 0),
            "cost_usd": None if cost_usd is None else float(cost_usd),
            "origin": str(origin or ""),
        }
        db = self._connect()
        try:
            cur = db.execute(
                f"INSERT INTO delegations ({','.join(_COLUMNS)}) "
                f"VALUES ({','.join(':' + c for c in _COLUMNS)})",
                row,
            )
            db.commit()
            return int(cur.lastrowid or 0) or None
        except sqlite3.Error:
            log.exception("[ledger] write failed")
            return None
        finally:
            db.close()

    def recent(self, limit: int = 100, *, session_id: str = "") -> list[dict]:
        """Newest edges first, optionally scoped to one originating session."""
        limit = max(1, min(int(limit), 1000))
        sql = "SELECT * FROM delegations"
        args: list[object] = []
        if session_id:
            sql += " WHERE session_id = ?"
            args.append(session_id)
        sql += " ORDER BY edge_id DESC LIMIT ?"
        args.append(limit)
        db = self._connect()
        try:
            return [dict(r) for r in db.execute(sql, args).fetchall()]
        except sqlite3.Error:
            log.exception("[ledger] read failed")
            return []
        finally:
            db.close()

    def edges(self, *, since_days: int | None = None) -> list[dict]:
        """Aggregated edges for the org chart: one row per (kind, name) target.

        This is the shape the visualization wants — the graph draws one edge per target,
        not one per dispatch. ``ok``/``failed`` are counted separately so a target that is
        reachable but failing every call is distinguishable from an idle one, which is the
        distinction a health dot alone cannot make.

        ``cost_usd`` sums only the rows that HAVE a cost; ``priced`` says how many those
        were, so a caller can tell "cheap" from "mostly unmeasured" instead of reading a
        partial sum as a total.
        """
        sql = """
            SELECT to_kind, to_name, to_instance,
                   COUNT(*)                                        AS dispatches,
                   SUM(CASE WHEN outcome = 'ok' THEN 1 ELSE 0 END) AS ok,
                   SUM(CASE WHEN outcome = 'failed' THEN 1 ELSE 0 END) AS failed,
                   SUM(CASE WHEN outcome = 'cancelled' THEN 1 ELSE 0 END) AS cancelled,
                   SUM(COALESCE(duration_ms, 0))                   AS duration_ms,
                   SUM(cost_usd)                                   AS cost_usd,
                   SUM(CASE WHEN cost_usd IS NOT NULL THEN 1 ELSE 0 END) AS priced,
                   MAX(at)                                         AS last_at
            FROM delegations
        """
        args: list[object] = []
        if since_days is not None:
            sql += " WHERE at >= ?"
            args.append((datetime.now(UTC) - timedelta(days=int(since_days))).isoformat())
        sql += " GROUP BY to_kind, to_name, to_instance ORDER BY dispatches DESC"
        db = self._connect()
        try:
            return [dict(r) for r in db.execute(sql, args).fetchall()]
        except sqlite3.Error:
            log.exception("[ledger] aggregate failed")
            return []
        finally:
            db.close()

    def iter_rows(self) -> Iterator[dict]:
        db = self._connect()
        try:
            for row in db.execute("SELECT * FROM delegations ORDER BY edge_id"):
                yield dict(row)
        finally:
            db.close()

    def prune(self, keep_days: int) -> int:
        """Drop edges older than ``keep_days``. Returns rows removed."""
        cutoff = (datetime.now(UTC) - timedelta(days=int(keep_days))).isoformat()
        db = self._connect()
        try:
            cur = db.execute("DELETE FROM delegations WHERE at < ?", (cutoff,))
            db.commit()
            return int(cur.rowcount or 0)
        except sqlite3.Error:
            log.exception("[ledger] prune failed")
            return 0
        finally:
            db.close()
