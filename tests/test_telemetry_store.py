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


def test_percentile_helper():
    assert _percentile([], 50) == 0
    assert _percentile([10], 95) == 10
    assert _percentile([1, 2, 3, 4, 5, 6, 7, 8, 9, 10], 50) in (5, 6)
    assert _percentile([1, 2, 3, 4, 5, 6, 7, 8, 9, 10], 95) == 10


@pytest.fixture
def telemetry_holder(store):
    """Point a2a_handler's telemetry holder at the test store, then restore."""
    import a2a_handler

    prev_store, prev_model = a2a_handler._TELEMETRY[0], a2a_handler._TELEMETRY_MODEL[0]
    a2a_handler._TELEMETRY[0] = store
    a2a_handler._TELEMETRY_MODEL[0] = "claude-opus-4-8"
    try:
        yield a2a_handler
    finally:
        a2a_handler._TELEMETRY[0] = prev_store
        a2a_handler._TELEMETRY_MODEL[0] = prev_model


def test_record_telemetry_writes_row_from_task_record(store, telemetry_holder):
    """The terminal writer maps a TaskRecord → a telemetry row (ADR 0006)."""
    a2a_handler = telemetry_holder
    rec = a2a_handler.TaskRecord(
        id="task-x", context_id="sess-1", state=a2a_handler.COMPLETED,
        created_at="2026-06-01T00:00:00+00:00", updated_at="2026-06-01T00:00:03+00:00",
        message_text="hi",
    )
    rec.usage = {
        "input_tokens": 1200, "output_tokens": 300, "total_tokens": 1500,
        "cache_read_input_tokens": 600, "cache_creation_input_tokens": 0, "cost_usd": 0.042,
    }
    rec.llm_calls = 3
    rec.tool_calls = 2

    a2a_handler._record_telemetry(rec)

    turns = store.recent()
    assert len(turns) == 1
    row = turns[0]
    assert row["task_id"] == "task-x"
    assert row["session_id"] == "sess-1"
    assert row["success"] == 1
    assert row["model"] == "claude-opus-4-8"
    assert row["cost_usd"] == 0.042
    assert row["llm_calls"] == 3 and row["tool_calls"] == 2
    assert row["duration_ms"] == 3000  # 3s between created/ended


def test_record_telemetry_noop_when_store_unset():
    import a2a_handler

    prev = a2a_handler._TELEMETRY[0]
    a2a_handler._TELEMETRY[0] = None
    try:
        rec = a2a_handler.TaskRecord(
            id="t", context_id="c", state=a2a_handler.COMPLETED,
            created_at="2026-06-01T00:00:00+00:00", updated_at="2026-06-01T00:00:01+00:00",
            message_text="x",
        )
        a2a_handler._record_telemetry(rec)  # must not raise
    finally:
        a2a_handler._TELEMETRY[0] = prev


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
