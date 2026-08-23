"""Tests for telemetry_store.py (ADR 0006 Slice 2 — per-turn rollups)."""

from __future__ import annotations

import pytest

from observability.telemetry_store import TelemetryStore, _percentile


@pytest.fixture
def store(tmp_path):
    return TelemetryStore(str(tmp_path / "telemetry.db"))


def _row(task_id, **over):
    base = dict(
        task_id=task_id,
        session_id="s1",
        state="completed",
        success=1,
        model="claude-opus-4-8",
        input_tokens=1000,
        output_tokens=200,
        total_tokens=1200,
        cache_read_input_tokens=400,
        cache_creation_input_tokens=0,
        cost_usd=0.03,
        duration_ms=2000,
        llm_calls=2,
        tool_calls=1,
        created_at="2026-06-01T00:00:00+00:00",
        ended_at="2026-06-01T00:00:02+00:00",
    )
    base.update(over)
    return base


def test_record_and_recent(store):
    store.record(_row("t1"))
    store.record(_row("t2", ended_at="2026-06-01T00:01:00+00:00"))
    recent = store.recent(limit=10)
    assert [r["task_id"] for r in recent] == ["t2", "t1"]  # newest first
    assert recent[0]["cost_usd"] == 0.03


def test_record_keeps_every_leg_of_a_task(store):
    # #3001: one row per turn LEG, not per task. `task_id` used to be the PRIMARY
    # KEY with an upsert, so the second record() silently replaced the first.
    store.record(_row("t1", cost_usd=0.01))
    store.record(_row("t1", cost_usd=0.05))
    recent = store.recent()
    assert len(recent) == 2
    assert sorted(r["cost_usd"] for r in recent) == [0.01, 0.05]


def test_rows_carry_a_stable_surrogate_id(store):
    # The console keys its table rows on this. `task_id` cannot serve — two legs of
    # one HITL turn share it, and duplicate React keys break rendering (#3001).
    store.record(_row("t1"))
    store.record(_row("t1"))
    ids = [r["row_id"] for r in store.recent()]
    assert len(set(ids)) == 2 and all(isinstance(i, int) for i in ids)


def test_hitl_park_and_resume_are_two_rows_that_sum(store):
    """#3001 / #2943 — the audit's reproduction, as a regression test.

    A parked leg and the resume that follows carry the SAME A2A task id. Both are
    real turns with real spend, and the park leg is where an approval-gated turn
    does all its tool work — so collapsing them lost the tool calls entirely.
    """
    store.record(
        _row(
            "task-1",
            state="input_required",
            success=None,
            input_tokens=40,
            output_tokens=5,
            total_tokens=45,
            cost_usd=0.001,
            tool_calls=2,
            ended_at="2026-06-01T00:00:01+00:00",
        )
    )
    store.record(
        _row(
            "task-1",
            state="completed",
            success=1,
            input_tokens=70,
            output_tokens=30,
            total_tokens=100,
            cost_usd=0.004,
            tool_calls=0,
            ended_at="2026-06-01T00:00:03+00:00",
        )
    )
    assert len(store.recent()) == 2
    s = store.summary()
    assert s["input_tokens"] == 110  # not 70 — the park leg's prompt tokens survive
    assert s["cost_usd"] == 0.005  # not 0.004
    assert s["tool_calls"] == 2  # not 0 — every pre-approval tool call was erased


def test_pre_3001_store_migrates_without_losing_rows(tmp_path):
    # The pre-#3001 shape had `task_id TEXT PRIMARY KEY`; SQLite cannot alter a
    # primary key, so opening such a store rebuilds the table. Existing history
    # must survive that rebuild, and the new per-leg semantics must apply after it.
    import sqlite3

    path = str(tmp_path / "old.db")
    cols = (
        "task_id TEXT PRIMARY KEY, session_id TEXT, state TEXT, success INTEGER, model TEXT, "
        "models TEXT, input_tokens INTEGER, output_tokens INTEGER, total_tokens INTEGER, "
        "cache_read_input_tokens INTEGER, cache_creation_input_tokens INTEGER, cost_usd REAL, "
        "duration_ms INTEGER, llm_calls INTEGER, tool_calls INTEGER, created_at TEXT, ended_at TEXT"
    )
    db = sqlite3.connect(path)
    db.execute(f"CREATE TABLE turns ({cols})")
    db.execute(
        "INSERT INTO turns (task_id, state, model, input_tokens, cost_usd, ended_at) "
        "VALUES ('legacy-1', 'completed', 'm', 11, 0.02, '2026-05-01T00:00:00+00:00')"
    )
    db.commit()
    db.close()

    store = TelemetryStore(path)
    rows = store.recent()
    assert [r["task_id"] for r in rows] == ["legacy-1"]  # history preserved
    assert rows[0]["input_tokens"] == 11 and rows[0]["cost_usd"] == 0.02
    assert isinstance(rows[0]["row_id"], int)  # backfilled a surrogate id

    # Re-opening is a no-op, and the new semantics hold on the migrated store.
    TelemetryStore(path)
    store.record(_row("legacy-1", cost_usd=0.07))
    assert len(store.recent()) == 2


def test_record_persists_soul_rev(store):
    # #1691: the persona (SOUL.md) revision live for the turn round-trips.
    store.record(_row("t1", soul_rev="a1b2c3d4"))
    assert store.recent()[0]["soul_rev"] == "a1b2c3d4"


def test_soul_rev_migrates_onto_an_older_db(tmp_path):
    # A store created before soul_rev existed gets the column via the guarded ALTER on open,
    # so a soul_rev row then round-trips (same pattern as the earlier `models` migration).
    import sqlite3

    path = str(tmp_path / "old.db")
    cols = (
        "task_id TEXT PRIMARY KEY, session_id TEXT, state TEXT, success INTEGER, model TEXT, "
        "models TEXT, input_tokens INTEGER, output_tokens INTEGER, total_tokens INTEGER, "
        "cache_read_input_tokens INTEGER, cache_creation_input_tokens INTEGER, cost_usd REAL, "
        "duration_ms INTEGER, llm_calls INTEGER, tool_calls INTEGER, created_at TEXT, ended_at TEXT"
    )
    db = sqlite3.connect(path)
    db.execute(f"CREATE TABLE turns ({cols})")
    db.commit()
    db.close()

    store = TelemetryStore(path)  # _init_db runs the guarded ALTER
    store.record(_row("t1", soul_rev="deadbeef"))
    assert store.recent()[0]["soul_rev"] == "deadbeef"


def test_context_tokens_roundtrips_and_migrates(tmp_path):
    # #2773 / ADR 0101 D6: the per-turn context-window fill persists, and a store
    # created before the column existed gains it via the same guarded ALTER as
    # soul_rev above (the old-schema table here predates ALL migrated columns).
    import sqlite3

    path = str(tmp_path / "old.db")
    cols = (
        "task_id TEXT PRIMARY KEY, session_id TEXT, state TEXT, success INTEGER, model TEXT, "
        "models TEXT, input_tokens INTEGER, output_tokens INTEGER, total_tokens INTEGER, "
        "cache_read_input_tokens INTEGER, cache_creation_input_tokens INTEGER, cost_usd REAL, "
        "duration_ms INTEGER, llm_calls INTEGER, tool_calls INTEGER, created_at TEXT, ended_at TEXT"
    )
    db = sqlite3.connect(path)
    db.execute(f"CREATE TABLE turns ({cols})")
    db.commit()
    db.close()

    store = TelemetryStore(path)
    store.record(_row("t1", context_tokens=110_500))
    assert store.recent()[0]["context_tokens"] == 110_500


def test_recent_derives_per_turn_cache_hit_ratio(store):
    # #2773 / #3003: cached reads / the WHOLE prompt. The three token columns are
    # disjoint as of #3003 (`input_tokens` is the uncached share), so the
    # denominator is input + cache_read + cache_creation — here 600 + 400 = 1000.
    # A zero-prompt row reads 0.0, never a ZeroDivisionError.
    store.record(_row("t1", input_tokens=600, cache_read_input_tokens=400))
    store.record(_row("t2", input_tokens=0, cache_read_input_tokens=0, ended_at="2026-06-01T00:02:00+00:00"))
    recent = {r["task_id"]: r for r in store.recent()}
    assert recent["t1"]["cache_hit_ratio"] == 0.4
    assert recent["t2"]["cache_hit_ratio"] == 0.0


def test_cache_hit_ratio_counts_cache_writes_in_the_prompt(store):
    # A cache WRITE is prompt the turn actually sent (at 1.25x), so it belongs in
    # the denominator — otherwise the first turn of a cached thread reports a
    # hit ratio computed against a prompt far smaller than the one it paid for.
    store.record(_row("t1", input_tokens=100, cache_read_input_tokens=300, cache_creation_input_tokens=600))
    assert store.recent()[0]["cache_hit_ratio"] == 0.3  # 300 / (100 + 300 + 600)


def test_summary_context_fill_stats_exclude_zero_rows(store):
    # #2773: max/p95 context fill — turns recorded before the column existed (or
    # with no usage) read as 0 and must not drag the series down.
    store.record(_row("t1", context_tokens=0))
    store.record(_row("t2", context_tokens=40_000, ended_at="2026-06-01T00:02:00+00:00"))
    store.record(_row("t3", context_tokens=90_000, ended_at="2026-06-01T00:03:00+00:00"))
    s = store.summary()
    assert s["max_context_tokens"] == 90_000
    assert s["p95_context_tokens"] > 0


def test_record_noop_without_task_id(store):
    store.record({"cost_usd": 1.0})  # no task_id
    assert store.recent() == []


def test_stream_rows_yields_in_order(store):
    """stream_rows yields rows in descending ended_at order (newest first)."""
    store.record(_row("t1", ended_at="2026-06-01T00:00:00+00:00"))
    store.record(_row("t2", ended_at="2026-06-03T00:00:00+00:00"))
    store.record(_row("t3", ended_at="2026-06-02T00:00:00+00:00"))
    rows = list(store.stream_rows())
    assert [r["task_id"] for r in rows] == ["t2", "t3", "t1"]


def test_stream_rows_since_filter(store):
    """stream_rows(since_iso=...) filters in SQL, returning only matching rows."""
    store.record(_row("old", ended_at="2026-05-01T00:00:00+00:00"))
    store.record(_row("mid", ended_at="2026-05-15T00:00:00+00:00"))
    store.record(_row("new", ended_at="2026-06-01T00:00:00+00:00"))
    rows = list(store.stream_rows(since_iso="2026-05-10T00:00:00+00:00"))
    ids = [r["task_id"] for r in rows]
    assert "new" in ids and "mid" in ids
    assert "old" not in ids


def test_summary_aggregates(store):
    store.record(_row("t1", cost_usd=0.02, input_tokens=1000, cache_read_input_tokens=500, duration_ms=1000, success=1))
    store.record(
        _row(
            "t2",
            cost_usd=0.04,
            input_tokens=3000,
            cache_read_input_tokens=0,
            duration_ms=3000,
            success=0,
            state="failed",
        )
    )
    s = store.summary()
    assert s["turns"] == 2
    assert s["cost_usd"] == 0.06
    assert s["input_tokens"] == 4000
    assert s["success_rate"] == 0.5
    # cache-hit ratio = cached reads / the whole prompt = 500 / (4000 + 500) (#3003)
    assert s["cache_hit_ratio"] == round(500 / 4500, 4)
    assert s["p50_duration_ms"] in (1000, 3000)
    assert s["p95_duration_ms"] == 3000
    assert s["p99_duration_ms"] == 3000


def test_summary_by_model(store):
    store.record(_row("t1", model="claude-opus-4-8", cost_usd=0.05))
    store.record(_row("t2", model="claude-haiku-4-5", cost_usd=0.001))
    s = store.summary()
    models = {m["model"]: m for m in s["by_model"]}
    assert models["claude-opus-4-8"]["cost_usd"] == 0.05
    # ordered by cost desc → opus first
    assert s["by_model"][0]["model"] == "claude-opus-4-8"


def test_summary_by_model_includes_per_model_duration_percentiles(store):
    # #2678 — p50/p95/p99 duration per model, computed from the SAME turns
    # already durably recorded (no new capture seam needed).
    store.record(_row("t1", model="claude-opus-4-8", duration_ms=1000))
    store.record(_row("t2", model="claude-opus-4-8", duration_ms=3000))
    store.record(_row("t3", model="claude-haiku-4-5", duration_ms=500))
    s = store.summary()
    models = {m["model"]: m for m in s["by_model"]}
    assert models["claude-opus-4-8"]["p50_duration_ms"] in (1000, 3000)
    assert models["claude-opus-4-8"]["p95_duration_ms"] == 3000
    assert models["claude-opus-4-8"]["p99_duration_ms"] == 3000
    assert models["claude-haiku-4-5"]["p50_duration_ms"] == 500


def test_summary_by_model_percentiles_handle_a_null_model_group(store):
    # A turn recorded with no model set groups under model=NULL — SQL equality
    # (`model = ?`) never matches NULL, so a naive per-model lookup would silently
    # report zero percentiles for that group despite it having recorded turns.
    store.record(_row("t1", model=None, duration_ms=2000))
    store.record(_row("t2", model=None, duration_ms=4000))
    s = store.summary()
    null_row = next(m for m in s["by_model"] if m["model"] is None)
    assert null_row["turns"] == 2
    assert null_row["p50_duration_ms"] in (2000, 4000)
    assert null_row["p95_duration_ms"] == 4000


def test_summary_by_model_percentiles_respect_since_filter(store):
    store.record(_row("old", model="claude-opus-4-8", duration_ms=9000, ended_at="2026-05-01T00:00:00+00:00"))
    store.record(_row("new", model="claude-opus-4-8", duration_ms=1000, ended_at="2026-06-01T00:00:00+00:00"))
    s = store.summary(since_iso="2026-05-15T00:00:00+00:00")
    models = {m["model"]: m for m in s["by_model"]}
    assert models["claude-opus-4-8"]["p50_duration_ms"] == 1000


def test_summary_by_tool(store):
    # #2697 — per-tool p50/p95/p99 duration, aggregated from the same per-turn
    # tool_durations JSON blob already durably recorded (no new capture seam).
    import json

    store.record(_row("t1", tool_durations=json.dumps({"web_search": [800, 2200]})))
    store.record(_row("t2", tool_durations=json.dumps({"web_search": [3000], "calculator": [20]})))
    s = store.summary()
    tools = {t["tool"]: t for t in s["by_tool"]}
    assert tools["web_search"]["calls"] == 3
    assert tools["web_search"]["p50_duration_ms"] in (800, 2200, 3000)
    assert tools["web_search"]["p95_duration_ms"] == 3000
    assert tools["calculator"]["calls"] == 1
    assert tools["calculator"]["p50_duration_ms"] == 20
    # sorted p95 descending — slowest tool first
    assert s["by_tool"][0]["tool"] == "web_search"


def test_summary_by_tool_skips_turns_with_no_tool_durations(store):
    # A turn with tool_durations NULL/empty (an older row, or a turn whose only
    # tool_end producer doesn't stamp a duration) contributes nothing — not an
    # error, not a phantom zero-duration sample.
    import json

    store.record(_row("t1", tool_durations=None))
    store.record(_row("t2", tool_durations=""))
    store.record(_row("t3", tool_durations=json.dumps({"web_search": [500]})))
    s = store.summary()
    assert len(s["by_tool"]) == 1
    assert s["by_tool"][0]["tool"] == "web_search"
    assert s["by_tool"][0]["calls"] == 1


def test_summary_by_tool_ignores_malformed_json(store):
    store.record(_row("t1", tool_durations="not json"))
    store.record(_row("t2", tool_durations="[1, 2, 3]"))  # valid JSON, wrong shape (not a dict)
    s = store.summary()
    assert s["by_tool"] == []


def test_summary_by_tool_respects_since_filter(store):
    import json

    store.record(
        _row(
            "old",
            tool_durations=json.dumps({"web_search": [9000]}),
            ended_at="2026-05-01T00:00:00+00:00",
        )
    )
    store.record(
        _row(
            "new",
            tool_durations=json.dumps({"web_search": [1000]}),
            ended_at="2026-06-01T00:00:00+00:00",
        )
    )
    s = store.summary(since_iso="2026-05-15T00:00:00+00:00")
    tools = {t["tool"]: t for t in s["by_tool"]}
    assert tools["web_search"]["calls"] == 1
    assert tools["web_search"]["p50_duration_ms"] == 1000


def test_tool_durations_migrates_onto_an_older_db(tmp_path):
    # A store created before tool_durations existed gets the column via the
    # guarded ALTER on open, so a tool_durations row then round-trips.
    import json
    import sqlite3

    path = str(tmp_path / "old.db")
    cols = (
        "task_id TEXT PRIMARY KEY, session_id TEXT, state TEXT, success INTEGER, model TEXT, "
        "models TEXT, input_tokens INTEGER, output_tokens INTEGER, total_tokens INTEGER, "
        "cache_read_input_tokens INTEGER, cache_creation_input_tokens INTEGER, cost_usd REAL, "
        "duration_ms INTEGER, llm_calls INTEGER, tool_calls INTEGER, created_at TEXT, ended_at TEXT"
    )
    db = sqlite3.connect(path)
    db.execute(f"CREATE TABLE turns ({cols})")
    db.commit()
    db.close()

    store = TelemetryStore(path)  # _init_db runs the guarded ALTER
    store.record(_row("t1", tool_durations=json.dumps({"web_search": [500]})))
    assert store.recent()[0]["tool_durations"] == json.dumps({"web_search": [500]})


def test_summary_since_filter(store):
    store.record(_row("old", ended_at="2026-05-01T00:00:00+00:00"))
    store.record(_row("new", ended_at="2026-06-01T00:00:00+00:00"))
    s = store.summary(since_iso="2026-05-15T00:00:00+00:00")
    assert s["turns"] == 1


def test_summary_empty(store):
    s = store.summary()
    assert s["turns"] == 0
    assert s["cost_usd"] == 0.0
    assert s["success_rate"] == 0.0
    assert s["cache_hit_ratio"] == 0.0
    assert s["by_model"] == []
    assert s["by_tool"] == []


def test_prune(store):
    store.record(_row("old", ended_at="2026-01-01T00:00:00+00:00"))
    store.record(_row("new", ended_at="2026-06-01T00:00:00+00:00"))
    import datetime

    removed = store.prune(keep_days=30, now=datetime.datetime(2026, 6, 1, tzinfo=datetime.timezone.utc))
    assert removed == 1
    assert [r["task_id"] for r in store.recent()] == ["new"]


def test_outliers_flags_expensive_and_slow_turns(store):
    # A baseline of cheap/fast turns + one expensive + one slow.
    for i in range(8):
        store.record(_row(f"base{i}", cost_usd=0.01, duration_ms=500))
    store.record(_row("pricey", cost_usd=0.20, duration_ms=500))  # ≥5× median cost
    store.record(_row("slow", cost_usd=0.01, duration_ms=9000))  # ≥5× median latency
    flagged = {f["task_id"]: f for f in store.outliers(cost_multiple=5, latency_multiple=5)}
    assert "pricey" in flagged and "slow" in flagged
    assert "base0" not in flagged
    assert any("cost" in r for r in flagged["pricey"]["reasons"])
    assert any("latency" in r for r in flagged["slow"]["reasons"])


def test_outliers_empty_store(store):
    assert store.outliers() == []


def test_cache_read_savings_usd():
    from observability import pricing

    # A cached read saves `CACHE_READ_DISCOUNT` of what that token would otherwise
    # cost at the model's input rate. Derived from the table, not a hardcoded rate:
    # this assertion previously pinned the literal 0.000015 and so had to be edited
    # in lockstep with every price correction (#3002).
    rate = pricing.rate_for("claude-opus-4-8")["input"]
    saved = pricing.cache_read_savings_usd("claude-opus-4-8", 10000)
    assert saved == round(10000 * rate * pricing.CACHE_READ_DISCOUNT, 6)
    assert pricing.cache_read_savings_usd("claude-opus-4-8", 0) == 0.0


def test_median_helper():
    from observability.telemetry_store import _median

    assert _median([]) == 0
    assert _median([5]) == 5
    assert _median([1, 3]) == 2
    assert _median([3, 1, 2]) == 2


@pytest.mark.parametrize(
    ("n", "pct", "expected"),
    [
        # Nearest rank over 1..n is ceil(pct/100 * n). The old form sat one rank
        # high, but only for some (n, pct) pairs — banker's rounding flipped the
        # error with the parity of the half-value, which is why a permissive
        # `in (5, 6)` assertion here let it survive (#3005).
        (10, 50, 5),  # was 6
        (10, 95, 10),
        (20, 95, 19),  # was 20
        (100, 50, 50),  # was already correct — the parity trap
        (100, 95, 95),  # was 96
        (100, 99, 99),  # was 100 — p99 returned the MAXIMUM
        (1, 50, 1),
        (1, 99, 1),
        (3, 50, 2),
    ],
)
def test_percentile_is_nearest_rank(n, pct, expected):
    assert _percentile(list(range(1, n + 1)), pct) == expected


def test_percentile_never_exceeds_the_sample(store):
    # p99 must be a percentile, not "the worst turn we saw".
    values = list(range(1, 101))
    assert _percentile(values, 99) < max(values)


def test_percentile_helper():
    assert _percentile([], 50) == 0
    assert _percentile([10], 95) == 10


@pytest.fixture
def telemetry_server(store):
    """Point server's telemetry holder at the test store, then restore.

    Telemetry recording moved from the hand-rolled handler to
    ``server._record_a2a_telemetry`` (fed a ``TurnOutcome`` by the executor's
    terminal hook). The configured lead model defaults from ``_graph_config``;
    we leave it unset so the primary model comes from the turn's actual models."""
    import server

    prev = server.STATE.telemetry_store
    server.STATE.telemetry_store = store
    try:
        yield server
    finally:
        server.STATE.telemetry_store = prev


def _outcome(**kw):
    from a2a_impl.executor import TurnOutcome

    return TurnOutcome(**kw)


def test_record_telemetry_writes_row_from_turn_outcome(store, telemetry_server):
    """The terminal writer maps a TurnOutcome → a telemetry row (ADR 0006)."""
    server = telemetry_server
    server._record_a2a_telemetry(
        _outcome(
            task_id="task-x",
            context_id="sess-1",
            state="completed",
            text="hi",
            usage={
                "input_tokens": 1200,
                "output_tokens": 300,
                "cache_read_input_tokens": 600,
                "cache_creation_input_tokens": 0,
            },
            cost_usd=0.042,
            duration_ms=3000,
            llm_calls=3,
            tool_calls=2,
            models=["claude-opus-4-8"],
        )
    )

    turns = store.recent()
    assert len(turns) == 1
    row = turns[0]
    assert row["task_id"] == "task-x"
    assert row["session_id"] == "sess-1"
    assert row["success"] == 1
    assert row["model"] == "claude-opus-4-8"
    assert row["total_tokens"] == 1500
    assert row["cost_usd"] == 0.042
    assert row["llm_calls"] == 3 and row["tool_calls"] == 2
    assert row["duration_ms"] == 3000


def test_record_telemetry_uses_actual_models(store, telemetry_server):
    """The primary model is the first one actually used this turn, and the
    distinct set is stored (ADR 0006 Slice 4b — routing proof)."""
    server = telemetry_server
    server._record_a2a_telemetry(
        _outcome(
            task_id="task-rt",
            context_id="s",
            state="completed",
            text="hi",
            usage={"input_tokens": 100, "output_tokens": 10},
            cost_usd=0.001,
            duration_ms=1000,
            models=["protolabs/reasoning", "claude-haiku-4-5"],
        )
    )
    row = store.recent()[0]
    assert row["model"] == "protolabs/reasoning"  # primary = first actual
    assert row["models"] == "protolabs/reasoning,claude-haiku-4-5"


def test_record_telemetry_writes_tool_durations(store, telemetry_server):
    """#2697 — TurnOutcome.tool_durations round-trips as a JSON TEXT column,
    the tool_durations sibling of the models test above."""
    import json

    server = telemetry_server
    server._record_a2a_telemetry(
        _outcome(
            task_id="task-tools",
            context_id="s",
            state="completed",
            text="hi",
            usage={"input_tokens": 100, "output_tokens": 10},
            cost_usd=0.001,
            duration_ms=1000,
            models=["claude-opus-4-8"],
            tool_durations={"web_search": [800, 2200], "calculator": [20]},
        )
    )
    row = store.recent()[0]
    assert json.loads(row["tool_durations"]) == {"web_search": [800, 2200], "calculator": [20]}


def test_record_telemetry_omits_tool_durations_when_empty(store, telemetry_server):
    # An empty dict (no duration-carrying tool_end this turn) stores NULL, not
    # "{}" — keeps the by-tool aggregation's `if not raw: continue` skip working
    # without special-casing the empty-object string.
    server = telemetry_server
    server._record_a2a_telemetry(
        _outcome(
            task_id="task-no-tools",
            context_id="s",
            state="completed",
            text="hi",
            usage={"input_tokens": 100, "output_tokens": 10},
            cost_usd=0.001,
            duration_ms=1000,
        )
    )
    row = store.recent()[0]
    assert row["tool_durations"] is None


def test_executor_accumulates_tool_durations():
    """#2697 — the executor collects tool_end frames carrying duration_ms into
    TurnOutcome.tool_durations, keyed by tool name, mirroring the models
    accumulation test above."""
    import asyncio

    from a2a.server.context import ServerCallContext
    from a2a.server.agent_execution import RequestContext
    from a2a.server.events.event_queue import EventQueueLegacy as EventQueue
    from a2a.types import Message, Part, Role, SendMessageRequest

    from a2a_impl.executor import ProtoAgentExecutor, set_terminal_hook

    captured = []
    set_terminal_hook(captured.append)

    async def stream(text, ctx, *, resume=False, caller_trace=None, **kwargs):
        yield ("tool_start", {"id": "1", "name": "web_search"})
        yield ("tool_end", {"id": "1", "name": "web_search", "output": "x", "duration_ms": 800})
        yield ("tool_start", {"id": "2", "name": "web_search"})
        yield ("tool_end", {"id": "2", "name": "web_search", "output": "x", "duration_ms": 2200})
        yield ("tool_start", {"id": "3", "name": "calculator"})
        yield ("tool_end", {"id": "3", "name": "calculator", "output": "4", "duration_ms": 20})
        # An unmeasured tool_end (no run_id at start, per on_tool_start's fallback) —
        # duration_ms == 0 must be excluded, not counted as a real sub-ms sample.
        yield ("tool_end", {"id": "4", "name": "calculator", "output": "0", "duration_ms": 0})
        yield ("done", "ok")

    async def run():
        q = EventQueue()
        req = SendMessageRequest(message=Message(message_id="m", role=Role.ROLE_USER, parts=[Part(text="hi")]))
        ctx = RequestContext(call_context=ServerCallContext(), request=req, task_id="t", context_id="c")
        await ProtoAgentExecutor(stream).execute(ctx, q)

    try:
        asyncio.run(run())
    finally:
        set_terminal_hook(None)

    assert captured[0].tool_durations == {"web_search": [800, 2200], "calculator": [20]}


def test_executor_counts_each_tool_call_once_despite_the_announce_then_finalize_pair():
    """server/chat.py legitimately announces a tool_start TWICE per real call for a
    streaming model — once early (tool-call name streamed, empty args, so the card
    shows "running" right away) and once more at on_chat_model_end (SAME id, full
    args, to fill the card in). Before this fix the executor's naive `tool_calls += 1`
    counted both, doubling the metric everywhere it's read (dashboard, /perf, cost
    correlations). Dedupe by tool_call id: two tool_start frames sharing an id count
    as ONE call; a call with no id (a legacy plain-string producer) still counts."""
    import asyncio

    from a2a.server.context import ServerCallContext
    from a2a.server.agent_execution import RequestContext
    from a2a.server.events.event_queue import EventQueueLegacy as EventQueue
    from a2a.types import Message, Part, Role, SendMessageRequest

    from a2a_impl.executor import ProtoAgentExecutor, set_terminal_hook

    captured = []
    set_terminal_hook(captured.append)

    async def stream(text, ctx, *, resume=False, caller_trace=None, **kwargs):
        # Two real calls, each announced+finalized (4 tool_start events, 2 distinct ids).
        yield ("tool_start", {"id": "1", "name": "read_file", "input": ""})
        yield ("tool_start", {"id": "1", "name": "read_file", "input": '{"path": "a.py"}'})
        yield ("tool_end", {"id": "1", "name": "read_file", "output": "x"})
        yield ("tool_start", {"id": "2", "name": "read_file", "input": ""})
        yield ("tool_start", {"id": "2", "name": "read_file", "input": '{"path": "b.py"}'})
        yield ("tool_end", {"id": "2", "name": "read_file", "output": "y"})
        # A legacy id-less producer still counts once, not zero.
        yield ("tool_start", "legacy string producer")
        yield ("tool_end", "done")
        yield ("done", "ok")

    async def run():
        q = EventQueue()
        req = SendMessageRequest(message=Message(message_id="m", role=Role.ROLE_USER, parts=[Part(text="hi")]))
        ctx = RequestContext(call_context=ServerCallContext(), request=req, task_id="t", context_id="c")
        await ProtoAgentExecutor(stream).execute(ctx, q)

    try:
        asyncio.run(run())
    finally:
        set_terminal_hook(None)

    assert captured[0].tool_calls == 3


def test_executor_accumulates_distinct_models_in_first_seen_order():
    """The executor records each distinct model once, in first-seen order, on
    the TurnOutcome — the routing-proof signal."""
    import asyncio

    from a2a.server.context import ServerCallContext
    from a2a.server.agent_execution import RequestContext
    from a2a.server.events.event_queue import EventQueueLegacy as EventQueue
    from a2a.types import Message, Part, Role, SendMessageRequest

    from a2a_impl.executor import ProtoAgentExecutor, set_terminal_hook

    captured = []
    set_terminal_hook(captured.append)

    async def stream(text, ctx, *, resume=False, caller_trace=None, **kwargs):
        yield ("usage", {"input_tokens": 10, "output_tokens": 5, "model": "m1"})
        yield ("usage", {"input_tokens": 10, "output_tokens": 5, "model": "m2"})
        yield ("usage", {"input_tokens": 10, "output_tokens": 5, "model": "m1"})  # dup
        yield ("done", "ok")

    async def run():
        q = EventQueue()
        req = SendMessageRequest(message=Message(message_id="m", role=Role.ROLE_USER, parts=[Part(text="hi")]))
        ctx = RequestContext(call_context=ServerCallContext(), request=req, task_id="t", context_id="c")
        await ProtoAgentExecutor(stream).execute(ctx, q)

    try:
        asyncio.run(run())
    finally:
        set_terminal_hook(None)

    assert captured[0].models == ["m1", "m2"]
    assert captured[0].llm_calls == 3


def test_record_tools_deferred_noop_when_disabled():
    from observability import metrics

    metrics.record_tools_deferred(5)  # disabled in tests → no-op, no error


def test_record_telemetry_noop_when_store_unset():
    import server

    prev = server.STATE.telemetry_store
    server.STATE.telemetry_store = None
    try:
        server._record_a2a_telemetry(
            _outcome(
                task_id="t",
                context_id="c",
                state="completed",
                text="x",
                usage={"input_tokens": 1, "output_tokens": 1},
                duration_ms=1000,
            )
        )  # must not raise
    finally:
        server.STATE.telemetry_store = prev


def test_config_parses_telemetry(tmp_path):
    from graph.config import LangGraphConfig

    p = tmp_path / "langgraph-config.yaml"
    p.write_text("telemetry:\n  enabled: false\n  db_path: /tmp/t.db\n")
    cfg = LangGraphConfig.from_yaml(p)
    assert cfg.telemetry_enabled is False
    assert cfg.telemetry_db_path == "/tmp/t.db"


def test_config_telemetry_default_on():
    from graph.config import LangGraphConfig

    assert LangGraphConfig().telemetry_enabled is True


def test_retention_config_default_is_bounded():
    from graph.config import LangGraphConfig

    assert LangGraphConfig().telemetry_retention_days == 90  # guardrail on by default


def test_park_and_resume_write_two_rows_through_the_real_writer(store, telemetry_server):
    """#3001 — the same park/resume, driven through ``_record_a2a_telemetry``.

    The #2943 regression test asserts the two ``TurnOutcome`` objects the terminal
    hook receives and stops there, so it stayed green while the store collapsed
    them. This asserts the durable end: the writer is fed both legs exactly as the
    executor feeds it, and both must survive.
    """
    server = telemetry_server
    common = dict(context_id="sess-hitl", text="", models=["claude-opus-4-8"])
    server._record_a2a_telemetry(
        _outcome(
            task_id="task-hitl",
            state="input_required",
            usage={"input_tokens": 40, "output_tokens": 5},
            cost_usd=0.001,
            duration_ms=900,
            llm_calls=1,
            tool_calls=2,
            **common,
        )
    )
    server._record_a2a_telemetry(
        _outcome(
            task_id="task-hitl",
            state="completed",
            usage={"input_tokens": 70, "output_tokens": 30},
            cost_usd=0.004,
            duration_ms=1200,
            llm_calls=1,
            tool_calls=0,
            **common,
        )
    )

    rows = store.recent()
    assert len(rows) == 2
    assert {r["state"] for r in rows} == {"input_required", "completed"}
    assert all(r["task_id"] == "task-hitl" for r in rows)  # still joinable to the task

    s = store.summary()
    assert (s["input_tokens"], s["output_tokens"]) == (110, 35)
    assert s["cost_usd"] == 0.005
    assert s["tool_calls"] == 2


def test_success_rate_excludes_turns_awaiting_a_human(store):
    """#3004 — a parked leg is neither a success nor a failure.

    #2943 wrote `success = NULL` for a parked leg so it would stay out of
    `SUM(success)`, but the denominator was `COUNT(*)`, which counts it. Any agent
    using approvals therefore showed a permanently depressed success rate — the
    metric watched for "is this agent healthy" instead tracked how often it asked.
    """
    store.record(_row("t1", state="completed", success=1))
    store.record(_row("t2", state="input_required", success=None))
    s = store.summary()
    assert s["turns"] == 2  # every recorded row
    assert s["resolved"] == 1  # ...but only one of them resolved
    assert s["success_rate"] == 1.0  # not 0.5 — nothing failed


def test_success_rate_still_counts_real_failures(store):
    store.record(_row("t1", state="completed", success=1))
    store.record(_row("t2", state="failed", success=0))
    store.record(_row("t3", state="input_required", success=None))
    s = store.summary()
    assert (s["turns"], s["resolved"]) == (3, 2)
    assert s["success_rate"] == 0.5


def test_success_rate_is_zero_when_nothing_has_resolved(store):
    # All parked: no basis for a rate. Must not divide by zero.
    store.record(_row("t1", state="input_required", success=None))
    s = store.summary()
    assert s["resolved"] == 0 and s["success_rate"] == 0.0
