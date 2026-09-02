"""Local telemetry store — per-turn cost/latency rollups (ADR 0006 Slice 2).

Each row carries accumulated token usage (incl. prompt-cache), USD cost,
wall-clock duration, LLM-call + tool-call counts, model, and outcome.
This is the *durable, queryable* half of observability inside protoAgent — the
substrate for "what was expensive/slow over time" and the flywheel's analysis
(Prometheus is live-scrape-only; Langfuse is opt-in/external).

One row per turn LEG, not per task: a HITL park/resume is two legs sharing one
A2A task id, and each owns its own spend (#3001). Row identity is the surrogate
``row_id``; ``task_id`` is an ordinary indexed column several rows may share.

Written through the shared writer ``server/turn_telemetry.py::record_turn`` — the A2A
executor's terminal hook (completed/failed/canceled/parked legs), the non-streaming
driver behind ``/v1`` and ``HOST.invoke()`` (#3000), and CLI coding-agent runs (#3015).
Best-effort: a write failure never breaks a turn. Instance-scoped via the path
the host resolves (ADR 0004). No TTL — history is the point; ``prune`` exists for
hosts that want to cap retention.
"""

from __future__ import annotations

import json
import logging
import math
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

#: How many sampled turns a model needs before `outliers()` compares its rows against
#: that model's own median instead of the median across everything. Three is not a
#: distribution, but the rule it feeds is a coarse 5× — and three samples are already
#: enough to know that a coding-agent run is minutes while a chat turn is seconds, which
#: is the confusion that filled the flag list (#3015).
_OUTLIER_MIN_COHORT = 3

#: Label for the instance-wide cost baseline `outliers()` falls back to, and keeps as a
#: second, absolute signal capped at one row per model. Named "priced" because zero-cost
#: rows are excluded from it — see the function's docstring for why they have to be (#3041).
_ALL_PRICED = "all priced turns"


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
                # the turn sent (#2773). Kept out of the schema: every operand is
                # already a column, so a derived key can't drift from them.
                d["cache_hit_ratio"] = _cache_hit_ratio(d)
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
                    COALESCE(SUM(success), 0)         AS successes,
                    COUNT(success)                    AS resolved
                FROM turns {where}
                """,
                params,
            ).fetchone()
            out = dict(agg)
            out["cost_usd"] = round(out.get("cost_usd", 0.0) or 0.0, 6)
            out["avg_duration_ms"] = int(out.get("avg_duration_ms", 0) or 0)
            # Success rate is over turns that RESOLVED, not every recorded row (#3004).
            # A parked leg writes `success = NULL` — it is neither a success nor a
            # failure, it is a turn waiting on a human (#2943). NULL already kept it
            # out of `SUM(success)`, but the denominator was `COUNT(*)`, which counts
            # it — so every approval-gated turn read as a failure and any agent using
            # HITL showed a permanently depressed success rate. `COUNT(success)` skips
            # NULLs, so parked legs now drop out of BOTH halves.
            resolved = out.get("resolved", 0) or 0
            out["success_rate"] = round((out.get("successes", 0) or 0) / resolved, 4) if resolved else 0.0
            # Kept alongside `turns` (every recorded row) so the gap is visible rather
            # than merely excluded: turns - resolved = legs parked awaiting a human.
            out["resolved"] = resolved
            # Cache-hit ratio over the window — same definition as the per-row one.
            out["cache_hit_ratio"] = _cache_hit_ratio(out)
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
                       COALESCE(SUM(total_tokens),0) AS total_tokens,
                       -- The three cache operands, PER LANE (#3342). A store-wide
                       -- ratio hides the case it exists to catch: one model caching
                       -- well carries the average while another bills full input
                       -- price on every call. Same disjoint columns `_cache_hit_ratio`
                       -- reads, so the per-lane figure and the rollup can't drift.
                       COALESCE(SUM(input_tokens),0) AS input_tokens,
                       COALESCE(SUM(cache_read_input_tokens),0) AS cache_read_input_tokens,
                       COALESCE(SUM(cache_creation_input_tokens),0) AS cache_creation_input_tokens
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
            # Context fill per lane rides along on the same pass (#3342): judging
            # whether a model's prompts are even big enough to cache needs ITS OWN
            # fill, not the store's. Zero rows excluded for the same reason as the
            # turn-level series above — absent, not empty.
            fills_by_model: dict[str | None, list[int]] = {}
            for r in db.execute(f"SELECT model, duration_ms, context_tokens FROM turns {where}", params).fetchall():
                if r["duration_ms"] is not None:
                    durations_by_model.setdefault(r["model"], []).append(r["duration_ms"])
                if r["context_tokens"]:
                    fills_by_model.setdefault(r["model"], []).append(r["context_tokens"])
            by_model_out = []
            for row in by_model:
                model_durations = sorted(durations_by_model.get(row["model"], []))
                model_fills = sorted(fills_by_model.get(row["model"], []))
                by_model_out.append({
                    **dict(row),
                    "cost_usd": round(row["cost_usd"] or 0.0, 6),
                    "p50_duration_ms": _percentile(model_durations, 50),
                    "p95_duration_ms": _percentile(model_durations, 95),
                    "p99_duration_ms": _percentile(model_durations, 99),
                    "cache_hit_ratio": _cache_hit_ratio(dict(row)),
                    "p95_context_tokens": _percentile(model_fills, 95),
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
        self,
        *,
        cost_multiple: float = 5.0,
        latency_multiple: float = 5.0,
        sample: int = 200,
        limit: int = 20,
        min_cohort: int = _OUTLIER_MIN_COHORT,
    ) -> list[dict]:
        """Flag recent turns whose cost or duration is anomalous over the last ``sample``
        turns. Advise-only signal for the flywheel — read-only, no action taken. Each
        flagged turn carries a ``reasons`` list naming the baseline it beat. Newest first.

        **Latency is judged per model**, because the store holds several populations that
        are not comparable. A gateway chat turn is seconds; a CLI coding-agent run
        recorded under ``acp:<delegate>`` (#3015) is minutes, and against one shared
        median EVERY coder run clears 5× by construction — which silently filled all
        ``limit`` slots of an anomaly list whose whole job is to surface the few turns
        worth looking at, pushing the real gateway anomalies out of it. Comparing a
        turn against its own kind asks the question the panel claims to answer: not
        "is this slower than a chat turn" but "is this slow for what it is".

        **Cost is judged twice, and the second test names a MODEL rather than a turn.** A
        cohort median answers "unusual FOR THIS MODEL" and by construction cannot answer
        "this model is expensive" — past ``min_cohort`` priced rows a uniformly expensive
        model is measured against itself and goes quiet, so the third $40 run used to
        delete the flags the first two raised (#3041). More evidence, fewer alerts. A turn
        therefore also clears the bar by beating the median across every priced turn on the
        instance — but that absolute test may claim **at most one row per model**, the
        priciest turn sampled on it.

        The cap is the whole safety argument and it is not optional. A price-tier gap is
        ordinary: a mini model against a flagship clears 5× on its own with nothing wrong
        anywhere. Applied per turn, the absolute test therefore flags EVERY turn of the
        expensive tier — 60 flagship turns into a ``limit``-slot list is #3015's flood
        re-created on the cost axis, and it evicts the genuine runaway the test was added
        to keep. (An earlier cut of this called the test self-limiting because "a model can
        only clear 5× the population median while being a minority of that population".
        That is backwards. A model holding a majority of the priced rows *contains* the
        median and so can never clear it; being a minority is the flooding condition, and a
        45% minority of 200 rows is 90 rows.) One row per model is also the right
        cardinality: a uniformly expensive model has no anomalous turn, so four identical
        $40 rows tell an operator nothing the priciest one does not.

        What the cap costs, stated plainly: on an instance running two legitimate price
        tiers, the backstop names the expensive one once per sample — an ordinary flagship
        turn, correctly labelled as the priciest of N sampled turns on it. That is one
        slot, it is what "this model is expensive" looks like on a panel whose rows are
        turns, and it is the deliberate price of the cost signal not going silent.

        **Zero-cost rows are excluded from every cost median.** A coder run records cost 0
        — it bills its own subscription, not the gateway — and on a coder-dispatching
        instance those rows are most of the sample. Their median is structurally 0, and no
        multiplicative threshold can use it: the ``med_cost > 0`` guard just goes False and
        the cost half of this function stops flagging anything at all (#3041).

        A model with fewer than ``min_cohort`` rows in the sample has no baseline of its
        own and falls back — for cost to the priced-turn median, for latency to the median
        of its OWN KIND (``acp:`` coder run vs gateway turn), never the mixed sample.
        Deliberate: the first runs of a newly-configured model are exactly when a $40 turn
        should surface, and suppressing them until a cohort builds would hide it. The
        reason string names which baseline was used, so a flag is never ambiguous.

        What this deliberately does not answer is "which model costs the most in
        aggregate": that is ``summary()["by_model"]``, which the insights payload already
        carries next to this list.
        """
        recent = self.recent(limit=sample)
        if not recent:
            return []
        min_cohort = max(1, int(min_cohort))
        priced_median = _median([c for c in (_row_cost(r) for r in recent) if c > 0])
        cohorts: dict[str, list[dict]] = {}
        durations_by_kind: dict[str, list[int]] = {}
        for r in recent:
            cohorts.setdefault(str(r.get("model") or ""), []).append(r)
            durations_by_kind.setdefault(_turn_kind(r), []).append(_row_duration(r))
        fallback_dur = {kind: _median(values) for kind, values in durations_by_kind.items()}
        # (baseline, label) per model, resolved once per cohort rather than per row.
        cost_base: dict[str, tuple[float, str]] = {}
        dur_base: dict[str, tuple[float, str]] = {}
        # model -> (row_id of its priciest sampled turn, how many of its rows were priced).
        # Caps the absolute cost test at one row per model — see the docstring for why the
        # uncapped version is #3015's flood on the cost axis (#3041).
        backstop: dict[str, tuple[int, int]] = {}
        for model, rows in cohorts.items():
            label = model or "unknown model"
            # A cohort needs ``min_cohort`` PRICED rows before it can price itself: a model
            # whose sampled rows are mostly free says nothing about what an expensive turn
            # on it looks like.
            priced = [c for c in (_row_cost(r) for r in rows) if c > 0]
            cost_base[model] = (_median(priced), label) if len(priced) >= min_cohort else (priced_median, _ALL_PRICED)
            # The one row the absolute backstop may claim for this model. ``row_id`` is the
            # surrogate key (#3001) — a row without one cannot be singled out, so it never
            # takes the backstop rather than letting every row of the model match on None.
            priciest = max(rows, key=_row_cost)
            if _row_cost(priciest) > 0 and priciest.get("row_id") is not None:
                backstop[model] = (priciest["row_id"], len(priced))
            kind = _turn_kind(rows[0])  # one model, one kind
            dur_base[model] = (
                (_median([_row_duration(r) for r in rows]), label)
                if len(rows) >= min_cohort
                else (fallback_dur.get(kind, 0), f"all {kind} turns")
            )
        flagged = []
        for r in recent:
            model = str(r.get("model") or "")
            reasons = []
            cost, dur = _row_cost(r), _row_duration(r)
            med_cost, cost_label = cost_base[model]
            priciest_id, priced_seen = backstop.get(model, (None, 0))
            if med_cost > 0 and cost >= med_cost * cost_multiple:
                reasons.append(f"cost {cost:.4g} ≥ {cost_multiple:g}× {cost_label} median {med_cost:.4g}")
            elif (
                priced_median > 0
                and cost >= priced_median * cost_multiple
                and priciest_id is not None
                and priciest_id == r.get("row_id")
            ):
                # The absolute backstop. Reached only when the cohort baseline was the
                # model's OWN — where it falls back to `priced_median` the branch above has
                # already tested this exact predicate — and only on the model's priciest
                # sampled row, so it states "this model is expensive" once and can never
                # crowd out the anomalies (#3041).
                reasons.append(
                    f"cost {cost:.4g} ≥ {cost_multiple:g}× {_ALL_PRICED} median {priced_median:.4g}"
                    f" — priciest of {priced_seen} sampled {cost_label} turns"
                )
            med_dur, dur_label = dur_base[model]
            if med_dur > 0 and dur >= med_dur * latency_multiple:
                reasons.append(f"latency {dur}ms ≥ {latency_multiple:g}× {dur_label} median {med_dur}ms")
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


def _cache_hit_ratio(row: dict) -> float:
    """Cached reads / the turn's whole prompt (#3003).

    The denominator is ``input_tokens + cache_read + cache_creation`` because the
    three columns are DISJOINT as of #3003 — ``input_tokens`` is the uncached
    share only. Dividing by ``input_tokens`` alone (what this used to do, back
    when the column was cache-inclusive) would now report far above 1.0 on a
    cache-heavy turn.

    Rows written before #3003 still carry the cache-inclusive column, so their
    denominator double-counts the cached reads and the ratio reads a little low.
    Not backfilled, by decision: quietly rewriting recorded history is worse than
    a documented seam, and the direction is conservative.
    """
    cache_read = row.get("cache_read_input_tokens", 0) or 0
    prompt = (row.get("input_tokens", 0) or 0) + cache_read + (row.get("cache_creation_input_tokens", 0) or 0)
    return round(cache_read / prompt, 4) if prompt else 0.0


def _percentile(values: list[int], pct: float) -> int:
    """Nearest-rank percentile over a pre-sorted list (empty → 0).

    Nearest rank is ``ceil(pct/100 * n)``, 1-indexed. The previous form —
    ``round(pct/100 * n + 0.5) - 1`` — sat one rank high, but only sometimes:
    Python's ``round`` is banker's rounding, so the error flipped with the parity
    of the half-value. At n=100 that made p50 correct and p99 return the MAXIMUM,
    which is not a percentile at all — and p99 is the column an operator reads to
    decide whether a tool is slow (#3005).
    """
    if not values:
        return 0
    k = max(0, min(len(values) - 1, math.ceil(pct / 100.0 * len(values)) - 1))
    return int(values[k])


def _row_cost(row: dict) -> float:
    return float(row.get("cost_usd") or 0.0)


def _row_duration(row: dict) -> int:
    return int(row.get("duration_ms") or 0)


def _turn_kind(row: dict) -> str:
    """Which non-comparable population a row belongs to, for `outliers()` baselines.

    ``acp:<delegate>`` is this repo's marker for "this turn was not gateway-metered"
    (plugins/coding_agent/acp_client.py, server.chat._acp_drive_turn). Such a run takes
    minutes where a gateway turn takes seconds, so a model too new to have a baseline of
    its own falls back to the median of its OWN kind rather than the mixed sample —
    otherwise a first coder run is flagged for being a coder run (#3041, #3015).
    """
    return "coder" if str(row.get("model") or "").startswith("acp:") else "gateway"


def _median(values: list):
    """Median of an unsorted numeric list (empty → 0)."""
    if not values:
        return 0
    s = sorted(values)
    n = len(s)
    mid = n // 2
    return s[mid] if n % 2 else (s[mid - 1] + s[mid]) / 2
