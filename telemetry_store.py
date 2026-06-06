"""Local telemetry store — per-turn cost/latency rollups (ADR 0006 Slice 2).

One row per terminal A2A turn: accumulated token usage (incl. prompt-cache),
USD cost, wall-clock duration, LLM-call + tool-call counts, model, and outcome.
This is the *durable, queryable* half of observability inside protoAgent — the
substrate for "what was expensive/slow over time" and the flywheel's analysis
(Prometheus is live-scrape-only; Langfuse is opt-in/external).

Written from the single terminal chokepoint (``A2ATaskStore.update_state`` when
the state goes terminal), so completed/failed/canceled turns are all captured.
Best-effort: a write failure never breaks a turn. Instance-scoped via the path
the host resolves (ADR 0004). No TTL — history is the point; ``prune`` exists for
hosts that want to cap retention.
"""

from __future__ import annotations

import logging
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

log = logging.getLogger(__name__)

_COLUMNS = (
    "task_id", "session_id", "state", "success", "model", "models",
    "input_tokens", "output_tokens", "total_tokens",
    "cache_read_input_tokens", "cache_creation_input_tokens",
    "cost_usd", "duration_ms", "llm_calls", "tool_calls",
    "created_at", "ended_at",
)


class TelemetryStore:
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
                CREATE TABLE IF NOT EXISTS turns (
                    task_id                     TEXT PRIMARY KEY,
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
                    ended_at                    TEXT
                )
                """
            )
            db.execute("CREATE INDEX IF NOT EXISTS ix_turns_ended ON turns(ended_at)")
            # Lightweight migration for stores created before `models` existed
            # (ADR 0006 Slice 4b). ALTER is idempotent-guarded by the try/except.
            try:
                db.execute("ALTER TABLE turns ADD COLUMN models TEXT")
            except sqlite3.OperationalError:
                pass  # column already present
            db.commit()
        finally:
            db.close()

    def record(self, row: dict) -> None:
        """Upsert one per-turn telemetry row (keyed by task_id). Best-effort."""
        task_id = row.get("task_id")
        if not task_id:
            return
        values = [row.get(c) for c in _COLUMNS]
        placeholders = ",".join("?" for _ in _COLUMNS)
        cols = ",".join(_COLUMNS)
        updates = ",".join(f"{c}=excluded.{c}" for c in _COLUMNS if c != "task_id")
        db = self._connect()
        try:
            db.execute(
                f"INSERT INTO turns ({cols}) VALUES ({placeholders}) "
                f"ON CONFLICT(task_id) DO UPDATE SET {updates}",
                values,
            )
            db.commit()
        finally:
            db.close()

    def recent(self, limit: int = 50) -> list[dict]:
        """Most recent turns, newest first."""
        db = self._connect()
        try:
            rows = db.execute(
                "SELECT * FROM turns ORDER BY ended_at DESC LIMIT ?",
                (max(1, int(limit)),),
            ).fetchall()
            return [dict(r) for r in rows]
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
                r[0] for r in db.execute(
                    f"SELECT duration_ms FROM turns {where} ORDER BY duration_ms", params
                ).fetchall() if r[0] is not None
            ]
            out["p50_duration_ms"] = _percentile(durations, 50)
            out["p95_duration_ms"] = _percentile(durations, 95)
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
            out["by_model"] = [
                {**dict(r), "cost_usd": round(r["cost_usd"] or 0.0, 6)} for r in by_model
            ]
            return out
        finally:
            db.close()

    def outliers(self, *, cost_multiple: float = 5.0, latency_multiple: float = 5.0,
                 sample: int = 200, limit: int = 20) -> list[dict]:
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
