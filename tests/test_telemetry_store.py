"""Tests for telemetry_store.py (ADR 0006 Slice 2 — per-turn rollups)."""

from __future__ import annotations

import pytest

from telemetry_store import TelemetryStore, _percentile


@pytest.fixture
def store(tmp_path):
    return TelemetryStore(str(tmp_path / "telemetry.db"))


def _row(task_id, **over):
    base = dict(
        task_id=task_id, session_id="s1", state="completed", success=1,
        model="claude-opus-4-8", input_tokens=1000, output_tokens=200,
        total_tokens=1200, cache_read_input_tokens=400,
        cache_creation_input_tokens=0, cost_usd=0.03, duration_ms=2000,
        llm_calls=2, tool_calls=1, created_at="2026-06-01T00:00:00+00:00",
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


def test_record_upserts_by_task_id(store):
    store.record(_row("t1", cost_usd=0.01))
    store.record(_row("t1", cost_usd=0.05))  # same task_id → update, not dup
    recent = store.recent()
    assert len(recent) == 1
    assert recent[0]["cost_usd"] == 0.05


def test_record_noop_without_task_id(store):
    store.record({"cost_usd": 1.0})  # no task_id
    assert store.recent() == []


def test_summary_aggregates(store):
    store.record(_row("t1", cost_usd=0.02, input_tokens=1000, cache_read_input_tokens=500,
                       duration_ms=1000, success=1))
    store.record(_row("t2", cost_usd=0.04, input_tokens=3000, cache_read_input_tokens=0,
                       duration_ms=3000, success=0, state="failed"))
    s = store.summary()
    assert s["turns"] == 2
    assert s["cost_usd"] == 0.06
    assert s["input_tokens"] == 4000
    assert s["success_rate"] == 0.5
    # cache-hit ratio = cached reads / total input = 500 / 4000
    assert s["cache_hit_ratio"] == round(500 / 4000, 4)
    assert s["p50_duration_ms"] in (1000, 3000)
    assert s["p95_duration_ms"] == 3000


def test_summary_by_model(store):
    store.record(_row("t1", model="claude-opus-4-8", cost_usd=0.05))
    store.record(_row("t2", model="claude-haiku-4-5", cost_usd=0.001))
    s = store.summary()
    models = {m["model"]: m for m in s["by_model"]}
    assert models["claude-opus-4-8"]["cost_usd"] == 0.05
    # ordered by cost desc → opus first
    assert s["by_model"][0]["model"] == "claude-opus-4-8"


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
    store.record(_row("pricey", cost_usd=0.20, duration_ms=500))   # ≥5× median cost
    store.record(_row("slow", cost_usd=0.01, duration_ms=9000))    # ≥5× median latency
    flagged = {f["task_id"]: f for f in store.outliers(cost_multiple=5, latency_multiple=5)}
    assert "pricey" in flagged and "slow" in flagged
    assert "base0" not in flagged
    assert any("cost" in r for r in flagged["pricey"]["reasons"])
    assert any("latency" in r for r in flagged["slow"]["reasons"])


def test_outliers_empty_store(store):
    assert store.outliers() == []


def test_cache_read_savings_usd():
    import pricing

    # opus input rate 0.000015, discount 0.9 → 10000 cached reads save ~0.135
    saved = pricing.cache_read_savings_usd("claude-opus-4-8", 10000)
    assert saved == round(10000 * 0.000015 * 0.9, 6)
    assert pricing.cache_read_savings_usd("claude-opus-4-8", 0) == 0.0


def test_median_helper():
    from telemetry_store import _median

    assert _median([]) == 0
    assert _median([5]) == 5
    assert _median([1, 3]) == 2
    assert _median([3, 1, 2]) == 2


def test_percentile_helper():
    assert _percentile([], 50) == 0
    assert _percentile([10], 95) == 10
    assert _percentile([1, 2, 3, 4, 5, 6, 7, 8, 9, 10], 50) in (5, 6)
    assert _percentile([1, 2, 3, 4, 5, 6, 7, 8, 9, 10], 95) == 10


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
    from a2a_executor import TurnOutcome

    return TurnOutcome(**kw)


def test_record_telemetry_writes_row_from_turn_outcome(store, telemetry_server):
    """The terminal writer maps a TurnOutcome → a telemetry row (ADR 0006)."""
    server = telemetry_server
    server._record_a2a_telemetry(_outcome(
        task_id="task-x", context_id="sess-1", state="completed", text="hi",
        usage={
            "input_tokens": 1200, "output_tokens": 300,
            "cache_read_input_tokens": 600, "cache_creation_input_tokens": 0,
        },
        cost_usd=0.042, duration_ms=3000, llm_calls=3, tool_calls=2,
        models=["claude-opus-4-8"],
    ))

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
    server._record_a2a_telemetry(_outcome(
        task_id="task-rt", context_id="s", state="completed", text="hi",
        usage={"input_tokens": 100, "output_tokens": 10},
        cost_usd=0.001, duration_ms=1000,
        models=["protolabs/reasoning", "claude-haiku-4-5"],
    ))
    row = store.recent()[0]
    assert row["model"] == "protolabs/reasoning"          # primary = first actual
    assert row["models"] == "protolabs/reasoning,claude-haiku-4-5"


def test_executor_accumulates_distinct_models_in_first_seen_order():
    """The executor records each distinct model once, in first-seen order, on
    the TurnOutcome — the routing-proof signal."""
    import asyncio

    from a2a.server.context import ServerCallContext
    from a2a.server.agent_execution import RequestContext
    from a2a.server.events.event_queue import EventQueueLegacy as EventQueue
    from a2a.types import Message, Part, Role, SendMessageRequest

    from a2a_executor import ProtoAgentExecutor, set_terminal_hook

    captured = []
    set_terminal_hook(captured.append)

    async def stream(text, ctx, *, resume=False, caller_trace=None, **kwargs):
        yield ("usage", {"input_tokens": 10, "output_tokens": 5, "model": "m1"})
        yield ("usage", {"input_tokens": 10, "output_tokens": 5, "model": "m2"})
        yield ("usage", {"input_tokens": 10, "output_tokens": 5, "model": "m1"})  # dup
        yield ("done", "ok")

    async def run():
        q = EventQueue()
        req = SendMessageRequest(
            message=Message(message_id="m", role=Role.ROLE_USER, parts=[Part(text="hi")])
        )
        ctx = RequestContext(call_context=ServerCallContext(), request=req, task_id="t", context_id="c")
        await ProtoAgentExecutor(stream).execute(ctx, q)

    try:
        asyncio.run(run())
    finally:
        set_terminal_hook(None)

    assert captured[0].models == ["m1", "m2"]
    assert captured[0].llm_calls == 3


def test_record_tools_deferred_noop_when_disabled():
    import metrics

    metrics.record_tools_deferred(5)  # disabled in tests → no-op, no error


def test_record_telemetry_noop_when_store_unset():
    import server

    prev = server.STATE.telemetry_store
    server.STATE.telemetry_store = None
    try:
        server._record_a2a_telemetry(_outcome(
            task_id="t", context_id="c", state="completed", text="x",
            usage={"input_tokens": 1, "output_tokens": 1}, duration_ms=1000,
        ))  # must not raise
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
