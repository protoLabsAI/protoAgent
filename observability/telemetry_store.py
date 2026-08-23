"""Local telemetry store — per-turn cost/latency rollups (ADR 0006 Slice 2).

Each row carries accumulated token usage (incl. prompt-cache), USD cost,
wall-clock duration, LLM-call + tool-call counts, model, and outcome.
This is the *durable, queryable* half of observability inside protoAgent — the
substrate for "what was expensive/slow over time" and the flywheel's analysis
(Prometheus is live-scrape-only; Langfuse is opt-in/external).

One row per turn LEG, not per task: a HITL park/resume is two legs sharing one
A2A task id, and each owns its own spend (#3001). Row identity is the surrogate
``row_id``; ``task_id`` is an ordinary indexed column several rows may share.

Written from the single terminal chokepoint (``server.a2a._a2a_terminal``, the
executor's terminal hook), so completed/failed/canceled/parked turns are all
captured.
Best-effort: a write failure never breaks a turn. Instance-scoped via the path
the host resolves (ADR 0004). No TTL — history is the point; ``prune`` exists for
hosts that want to cap retention.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

log = logging.getLogger(__name__)

_COLUMNS = (
    "task_id",
    "session_id",
    "state",
    "success",
    "model",
    "models",
    "input_tokens",
    "output_tokens",
    "total_tokens",
    "cache_read_input_tokens",
    "cache_creation_input_tokens",
    "cost_usd",
    "duration_ms",
    "llm_calls",
    "tool_calls",
    "created_at",
    "ended_at",
    "soul_rev",  # short hash of the persona (SOUL.md) live for this turn (#1691)
    "trace_id",  # Langfuse trace for this turn — lets the console deep-link to the trace tree
    "tool_durations",  # JSON {tool_name: [ms, ...]} for this turn's calls (#2697)
    "context_tokens",  # peak single-call prompt size = context-window fill (#2773, ADR 0101 D6)
)


class TelemetryStore:
    def __init__(self, db_path: str) -> None:
        self.path = str(db_path)
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
                CREATE TABLE IF NOT EXISTS turns (
                    row_id                      INTEGER PRIMARY KEY AUTOINCREMENT,
                    task_id                     TEXT,
                    session_id                  TEXT,
                    state                       TEXT,
                    success                     INTEGER,
                    model                       TEXT,
                    models                      TEXT,
                    input_tokens                INTEGER DEFAULT 0,
                    output_tokens               INTEGER DEFAULT 0,
                    total_tokens                INTEGER DEFAULT 0,
                    cache_read_input_tokens     INTEGER DEFAULT 0,
                    cache_creation_input_tokens INTEGER DEFAULT 0,
                    cost_usd                    REAL    DEFAULT 0,
                    duration_ms                 INTEGER DEFAULT 0,
                    llm_calls                   INTEGER DEFAULT 0,
                    tool_calls                  INTEGER DEFAULT 0,
                    created_at                  TEXT,
                    ended_at                    TEXT,
                    soul_rev                    TEXT,
                    trace_id                    TEXT,
                    tool_durations               TEXT,
                    context_tokens              INTEGER DEFAULT 0
                )
                """
            )
            db.execute("CREATE INDEX IF NOT EXISTS ix_turns_ended ON turns(ended_at)")
            # Lightweight migrations for stores created before a column existed. Each ALTER is
            # idempotent-guarded by the try/except (fires once on an older DB, no-ops after).
            for _col, _type in (
                ("models", "TEXT"),  # ADR 0006 4b
                ("soul_rev", "TEXT"),  # #1691
                ("trace_id", "TEXT"),  # console→Langfuse pivot
                ("tool_durations", "TEXT"),  # #2697
                ("context_tokens", "INTEGER DEFAULT 0"),  # #2773 / ADR 0101 D6
            ):
                try:
                    db.execute(f"ALTER TABLE turns ADD COLUMN {_col} {_type}")
                except sqlite3.OperationalError:
                    pass  # column already present
            self._migrate_to_row_id(db)  # #3001 — must run after the ADD COLUMNs above
            db.execute("CREATE INDEX IF NOT EXISTS ix_turns_task ON turns(task_id)")
            db.commit()
        finally:
            db.close()

    @staticmethod
    def _migrate_to_row_id(db: sqlite3.Connection) -> None:
        """Rebuild a pre-#3001 store, where ``task_id`` was the PRIMARY KEY.

        One task is not one turn. A HITL park/resume is two legs — two ``execute()``
        calls, each with its own model calls, tool calls and wall clock — and A2A
        gives both the SAME task id. Under the old ``ON CONFLICT(task_id) DO UPDATE``
        the resumed leg overwrote the parked one, so a turn that paused for approval
        reported only what happened AFTER the human answered: every pre-approval tool
        call and its tokens silently disappeared (#3001, #2943).

        The row identity is now a surrogate, and ``task_id`` is an ordinary indexed
        column that several rows may share. That is also what lets a turn with no
        task id at all be recorded (#3000).

        SQLite cannot alter a primary key, so this is the standard rebuild: create
        the new shape, copy, swap. Guarded on the absence of ``row_id``, so it fires
        once per old store and no-ops forever after. Column list comes from the live
        table, not a hardcoded one, so a store missing a late-added column still
        copies cleanly.
        """
        cols = [r["name"] for r in db.execute("PRAGMA table_info(turns)").fetchall()]
        if not cols or "row_id" in cols:
            return  # fresh store (already the new shape) or already migrated
        carried = ",".join(c for c in cols if c != "row_id")
        log.info("[telemetry] migrating store to per-leg rows (#3001)")
        db.execute("ALTER TABLE turns RENAME TO turns_pre3001")
        db.execute(
            """
            CREATE TABLE turns (
                row_id                      INTEGER PRIMARY KEY AUTOINCREMENT,
                task_id                     TEXT,
                session_id                  TEXT,
                state                       TEXT,
                success                     INTEGER,
                model                       TEXT,
                models                      TEXT,
                input_tokens                INTEGER DEFAULT 0,
                output_tokens               INTEGER DEFAULT 0,
                total_tokens                INTEGER DEFAULT 0,
                cache_read_input_tokens     INTEGER DEFAULT 0,
                cache_creation_input_tokens INTEGER DEFAULT 0,
                cost_usd                    REAL    DEFAULT 0,
                duration_ms                 INTEGER DEFAULT 0,
                llm_calls                   INTEGER DEFAULT 0,
                tool_calls                  INTEGER DEFAULT 0,
                created_at                  TEXT,
                ended_at                    TEXT,
                soul_rev                    TEXT,
                trace_id                    TEXT,
                tool_durations              TEXT,
                context_tokens              INTEGER DEFAULT 0
            )
            """
        )
        db.execute(f"INSERT INTO turns ({carried}) SELECT {carried} FROM turns_pre3001")
        db.execute("DROP TABLE turns_pre3001")
        # The old index rode the renamed table and was dropped with it.
        db.execute("CREATE INDEX IF NOT EXISTS ix_turns_ended ON turns(ended_at)")

    def record(self, row: dict) -> None:
        """Append one per-turn telemetry row. Best-effort.

        An INSERT, not an upsert: one row per turn LEG. Both legs of a HITL
        park/resume carry the same ``task_id``, so upserting on it silently
        replaced the parked leg's spend with the resumed leg's (#3001).
        """
        if not row.get("task_id"):
            return
        values = [row.get(c) for c in _COLUMNS]
        placeholders = ",".join("?" for _ in _COLUMNS)
        cols = ",".join(_COLUMNS)
        db = self._connect()
        try:
            db.execute(f"INSERT INTO turns ({cols}) VALUES ({placeholders})", values)
            db.commit()
        finally:
            db.close()

    def recent(self, limit: int = 50) -> list[dict]:
        """Most recent turns, newest first."""
        db = self._connect()
        try:
            rows = db.execute(
                "SELECT * FROM turns ORDER BY ended_at DESC, row_id DESC LIMIT ?",
                (max(1, int(limit)),),
            ).fetchall()
            out = []
            for r in rows:
                d = dict(r)
                # Per-turn cache-hit ratio, derived — cached reads / total prompt tokens
                # the turn sent (#2773). Kept out of the schema: both operands are
                # already columns, so a derived key can't drift from them.
                inp = d.get("input_tokens", 0) or 0
                d["cache_hit_ratio"] = round((d.get("cache_read_input_tokens", 0) or 0) / inp, 4) if inp else 0.0
                out.append(d)
            return out
        finally:
            db.close()

    def stream_rows(self, since_iso: str | None = None) -> Iterator[dict]:
        """Yield rows one at a time from the cursor — for streaming exports.

        Filters by ``ended_at >= since_iso`` in SQL (not Python post-filter).
        Does NOT materialize all rows into a list."""
        where, params = "", []
        if since_iso:
            where, params = "WHERE ended_at >= ?", [since_iso]
        db = self._connect()
        try:
            cur = db.execute(
                f"SELECT * FROM turns {where} ORDER BY ended_at DESC, row_id DESC",
                params,
            )
            for row in cur:
                yield dict(row)
        finally:
            db.close()

    def summary(self, since_iso: str | None = None) -> dict:
        """Aggregate rollup over all turns (or those ended at/after ``since_iso``):
        totals, averages, success rate, cache-hit ratio, and a per-model split."""
        where, params = "", []
        if since_iso:
            where, params = "WHERE ended_at >= ?", [since_iso]
        db = self._connect()
        try:
            agg = db.execute(
                f"""
                SELECT
                    COUNT(*)                          AS turns,
                    COALESCE(SUM(input_tokens), 0)    AS input_tokens,
                    COALESCE(SUM(output_tokens), 0)   AS output_tokens,
                    COALESCE(SUM(total_tokens), 0)    AS total_tokens,
                    COALESCE(SUM(cache_read_input_tokens), 0)     AS cache_read_input_tokens,
                    COALESCE(SUM(cache_creation_input_tokens), 0) AS cache_creation_input_tokens,
                    COALESCE(SUM(cost_usd), 0.0)      AS cost_usd,
                    COALESCE(SUM(llm_calls), 0)       AS llm_calls,
                    COALESCE(SUM(tool_calls), 0)      AS tool_calls,
                    COALESCE(AVG(duration_ms), 0)     AS avg_duration_ms,
                    COALESCE(SUM(success), 0)         AS successes
                FROM turns {where}
                """,
                params,
            ).fetchone()
            out = dict(agg)
            turns = out.get("turns", 0) or 0
            out["cost_usd"] = round(out.get("cost_usd", 0.0) or 0.0, 6)
            out["avg_duration_ms"] = int(out.get("avg_duration_ms", 0) or 0)
            out["success_rate"] = round((out.get("successes", 0) or 0) / turns, 4) if turns else 0.0
            # Cache-hit ratio: cached reads / total input tokens seen.
            inp = out.get("input_tokens", 0) or 0
            out["cache_hit_ratio"] = round((out.get("cache_read_input_tokens", 0) or 0) / inp, 4) if inp else 0.0
            # Latency percentiles (Python-side; bounded by typical volumes).
            durations = [
                r[0]
                for r in db.execute(f"SELECT duration_ms FROM turns {where} ORDER BY duration_ms", params).fetchall()
                if r[0] is not None
            ]
            out["p50_duration_ms"] = _percentile(durations, 50)
            out["p95_duration_ms"] = _percentile(durations, 95)
            out["p99_duration_ms"] = _percentile(durations, 99)  # #2678
            # Context-fill series (#2773, ADR 0101 D6) — same Python-side pattern as
            # the latency percentiles. Zero rows (pre-migration turns) are excluded:
            # a turn recorded before the column existed reads as 0, not as evidence
            # the thread was empty.
            fills = [
                r[0]
                for r in db.execute(
                    f"SELECT context_tokens FROM turns {where} ORDER BY context_tokens", params
                ).fetchall()
                if r[0]
            ]
            out["max_context_tokens"] = fills[-1] if fills else 0
            out["p95_context_tokens"] = _percentile(fills, 95)
            by_model = db.execute(
                f"""
                SELECT model,
                       COUNT(*)                     AS turns,
                       COALESCE(SUM(cost_usd), 0.0)  AS cost_usd,
                       COALESCE(SUM(total_tokens),0) AS total_tokens
                FROM turns {where}
                GROUP BY model ORDER BY cost_usd DESC
                """,
                params,
            ).fetchall()
            # #2678 — durable p50/p95/p99 duration PER MODEL, not just the whole-turn
            # figures above. Uses the SAME `duration_ms`/`model` columns the turn-level
            # percentiles above already read — no new capture seam or schema needed,
            # since `model` is already recorded per turn. (Per-TOOL percentiles would
            # need one: `tool_calls` is a per-turn count today, not which tools ran —
            # tracked separately, not attempted here.)
            # One query for every row's (model, duration_ms), grouped in Python — avoids
            # an N+1 query per model, and sidesteps `model = ?` never matching a NULL
            # model group (SQL equality never matches NULL; grouping in Python does).
            durations_by_model: dict[str | None, list[int]] = {}
            for r in db.execute(f"SELECT model, duration_ms FROM turns {where}", params).fetchall():
                if r["duration_ms"] is not None:
                    durations_by_model.setdefault(r["model"], []).append(r["duration_ms"])
            by_model_out = []
            for row in by_model:
                model_durations = sorted(durations_by_model.get(row["model"], []))
                by_model_out.append({
                    **dict(row),
                    "cost_usd": round(row["cost_usd"] or 0.0, 6),
                    "p50_duration_ms": _percentile(model_durations, 50),
                    "p95_duration_ms": _percentile(model_durations, 95),
                    "p99_duration_ms": _percentile(model_durations, 99),
                })
            out["by_model"] = by_model_out
            # #2697 — durable p50/p95/p99 duration PER TOOL, the by-model breakdown's
            # sibling. Unlike `model` (one scalar column, GROUP BY-able in SQL),
            # `tool_durations` is a per-turn JSON blob that can span several distinct
            # tools — so this is Python-side aggregation across every matching row's
            # blob, not a SQL GROUP BY. A turn with no duration-carrying tool_end (an
            # older row, or a tool_end producer that doesn't stamp one — see #2697)
            # has `tool_durations` NULL/empty and is skipped, not an error.
            durations_by_tool: dict[str, list[int]] = {}
            for r in db.execute(f"SELECT tool_durations FROM turns {where}", params).fetchall():
                raw = r["tool_durations"]
                if not raw:
                    continue
                try:
                    parsed = json.loads(raw)
                except (TypeError, ValueError):
                    continue
                if not isinstance(parsed, dict):
                    continue
                for tool_name, values in parsed.items():
                    if not isinstance(values, list):
                        continue
                    bucket = durations_by_tool.setdefault(tool_name, [])
                    bucket.extend(v for v in values if isinstance(v, int))
            by_tool_out = []
            for tool_name, values in durations_by_tool.items():
                tool_durations_sorted = sorted(values)
                by_tool_out.append({
                    "tool": tool_name,
                    "calls": len(tool_durations_sorted),
                    "p50_duration_ms": _percentile(tool_durations_sorted, 50),
                    "p95_duration_ms": _percentile(tool_durations_sorted, 95),
                    "p99_duration_ms": _percentile(tool_durations_sorted, 99),
                })
            # Slowest first — the whole point of this breakdown is "what's tool X's p99",
            # so lead with the tools worth looking at rather than the most-called ones.
            by_tool_out.sort(key=lambda row: row["p95_duration_ms"], reverse=True)
            out["by_tool"] = by_tool_out
            return out
        finally:
            db.close()

    def outliers(
        self, *, cost_multiple: float = 5.0, latency_multiple: float = 5.0, sample: int = 200, limit: int = 20
    ) -> list[dict]:
        """Flag recent turns whose cost or duration exceeds ``N×`` the median
        (over the last ``sample`` turns). Advise-only signal for the flywheel —
        read-only, no action taken. Each flagged turn carries a ``reasons`` list
        and the medians it beat. Newest first."""
        recent = self.recent(limit=sample)
        if not recent:
            return []
        med_cost = _median([float(r.get("cost_usd") or 0.0) for r in recent])
        med_dur = _median([int(r.get("duration_ms") or 0) for r in recent])
        flagged = []
        for r in recent:
            reasons = []
            cost = float(r.get("cost_usd") or 0.0)
            dur = int(r.get("duration_ms") or 0)
            if med_cost > 0 and cost >= med_cost * cost_multiple:
                reasons.append(f"cost {cost:.4g} ≥ {cost_multiple:g}× median {med_cost:.4g}")
            if med_dur > 0 and dur >= med_dur * latency_multiple:
                reasons.append(f"latency {dur}ms ≥ {latency_multiple:g}× median {med_dur}ms")
            if reasons:
                flagged.append({**r, "reasons": reasons})
            if len(flagged) >= limit:
                break
        return flagged

    def prune(self, keep_days: int = 30, *, now: datetime | None = None) -> int:
        """Delete turns older than ``keep_days``. Off by default — call from a
        host that wants bounded retention. Returns the rows removed."""
        now = now or datetime.now(UTC)
        cutoff = (now - timedelta(days=keep_days)).isoformat()
        db = self._connect()
        try:
            cur = db.execute("DELETE FROM turns WHERE ended_at < ?", (cutoff,))
            db.commit()
            return cur.rowcount
        finally:
            db.close()


def _percentile(values: list[int], pct: float) -> int:
    """Nearest-rank percentile over a pre-sorted list (empty → 0)."""
    if not values:
        return 0
    k = max(0, min(len(values) - 1, int(round((pct / 100.0) * len(values) + 0.5)) - 1))
    return int(values[k])


def _median(values: list):
    """Median of an unsorted numeric list (empty → 0)."""
    if not values:
        return 0
    s = sorted(values)
    n = len(s)
    mid = n // 2
    return s[mid] if n % 2 else (s[mid - 1] + s[mid]) / 2
