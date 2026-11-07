"""Content-free, nonblocking telemetry for both AuditMiddleware execution paths."""

import json
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import Event
from types import SimpleNamespace

import pytest

from graph import ledger
from graph.middleware import audit
from observability import tracing
from observability.telemetry_store import TelemetryStore
from runtime.state import STATE


def drain():
    audit._TELEMETRY_WRITER.submit(lambda: None).result(timeout=10)


def rows(store):
    with sqlite3.connect(store.path) as db:
        db.row_factory = sqlite3.Row
        return [dict(row) for row in db.execute("SELECT * FROM tool_calls")]


@pytest.fixture
def store(tmp_path, monkeypatch):
    drain()
    store = TelemetryStore(str(tmp_path / "telemetry.db"))
    monkeypatch.setattr(STATE, "telemetry_store", store)
    monkeypatch.setattr(STATE, "ledger_store", None, raising=False)
    monkeypatch.setattr(STATE, "plugin_meta", [
        {"id": "sample-plugin", "name": "A Display Name", "loaded": True, "tools": ["sample"]},
    ])
    monkeypatch.setattr(audit, "_TELEMETRY_FAILURE_LOGGED", False)
    monkeypatch.setattr(audit, "_observability", lambda: (audit._NoopAudit, tracing, audit._NoopMetrics))
    yield store
    drain()


async def invoke(mode, request, handler):
    middleware = audit.AuditMiddleware()
    if mode == "sync":
        return middleware.wrap_tool_call(request, handler)

    async def async_handler(req):
        return handler(req)

    return await middleware.awrap_tool_call(request, async_handler)


@pytest.mark.parametrize("mode", ["sync", "async"])
@pytest.mark.parametrize("outcome", ["ok", "error_result", "raises"])
@pytest.mark.parametrize("subagent", [False, True])
async def test_one_row_per_call(store, mode, outcome, subagent):
    from contextlib import nullcontext

    secret = "private-argument-value-unique-4786"
    request = SimpleNamespace(tool_call={"name": "sample", "args": {"value": secret}})
    content = "Error: unavailable" if outcome == "error_result" else "private-result-value-4786"
    result = SimpleNamespace(content=content)
    failure = ValueError("private-exception-value-4786")

    def handler(_request):
        if outcome == "raises":
            raise failure
        return result

    token = tracing._trace_id_ctx.set("test-trace")
    try:
        with ledger.run_scope(session_id="test-session"), (
            ledger.dispatch(to_kind="subagent", to_name="researcher") if subagent else nullcontext()
        ):
            if outcome == "raises":
                with pytest.raises(ValueError) as caught:
                    await invoke(mode, request, handler)
                assert caught.value is failure
            else:
                assert await invoke(mode, request, handler) is result
    finally:
        tracing._trace_id_ctx.reset(token)
    drain()
    recorded = rows(store)
    assert len(recorded) == 1
    row = recorded[0]
    assert row["ok"] == (outcome == "ok")
    assert row["error_class"] == {"ok": None, "error_result": "ToolError", "raises": "ValueError"}[outcome]
    assert row["plugin"] == "sample-plugin"
    assert row["session_id"] == "test-session"
    assert row["trace_id"] == "test-trace"
    assert row["subagent"] == ("researcher" if subagent else None)
    assert row["duration_ms"] >= 0
    assert row["args_chars"] == len(json.dumps(request.tool_call["args"], ensure_ascii=False))
    assert row["result_chars"] == (0 if outcome == "raises" else len(content))
    # Check both the main DB and any still-live WAL, not just the selected columns.
    for path in Path(store.path).parent.glob("telemetry.db*"):
        data = path.read_bytes()
        assert secret.encode() not in data
        assert b"private-result-value-4786" not in data
        assert b"private-exception-value-4786" not in data


@pytest.mark.parametrize("mode", ["sync", "async"])
async def test_write_failure_is_logged_once_and_preserves_result(store, monkeypatch, caplog, mode):
    def fail(row):
        raise RuntimeError("sensitive write error")

    monkeypatch.setattr(store, "record_tool_call", fail)
    request = SimpleNamespace(tool_call={"name": "sample", "args": {}})
    result = SimpleNamespace(content="unchanged")
    for _ in range(2):
        assert await invoke(mode, request, lambda req: result) is result
    drain()
    assert result.content == "unchanged"
    assert caplog.text.count("tool telemetry unavailable") == 1
    assert "sensitive write error" not in caplog.text


@pytest.mark.parametrize("mode", ["sync", "async"])
async def test_slow_store_does_not_delay_tool(store, monkeypatch, mode):
    entered, release = Event(), Event()
    original = store.record_tool_call

    def slow(row):
        entered.set()
        assert release.wait(timeout=10)
        original(row)

    monkeypatch.setattr(store, "record_tool_call", slow)
    request = SimpleNamespace(tool_call={"name": "core_tool", "args": {}})
    result = SimpleNamespace(content="ok")
    try:
        assert await invoke(mode, request, lambda req: result) is result
        assert entered.wait(timeout=5)
        assert rows(store) == []
    finally:
        release.set()
        drain()
    assert rows(store)[0]["plugin"] == "core"


def test_summary_and_prune(store):
    now = datetime.now(UTC)
    old = (now - timedelta(days=40)).isoformat()
    current = now.isoformat()
    for tool, ts, duration, ok in [("old", old, 999, 0)] + [
        ("sample", current, n, int(n % 10 != 0)) for n in range(1, 101)
    ] + [("other", current, 7, 1)]:
        store.record_tool_call({"tool": tool, "ts": ts, "duration_ms": duration, "ok": ok})
    assert store.tool_summary(current) == [
        {"tool": "other", "calls": 1, "errors": 0, "error_rate": 0, "p50_ms": 7, "p95_ms": 7},
        {"tool": "sample", "calls": 100, "errors": 10, "error_rate": .1, "p50_ms": 50, "p95_ms": 95},
    ]
    store.record({"task_id": "old-turn", "ended_at": old})
    assert store.prune(keep_days=30, now=now) == 2
    assert len(rows(store)) == 101
    assert store.recent() == []
    assert store.tool_summary((now + timedelta(days=1)).isoformat()) == []
    # Reopening an existing database is additive and idempotent.
    reopened = TelemetryStore(store.path)
    assert reopened.tool_summary() == store.tool_summary()
    with sqlite3.connect(store.path) as db:
        indexes = {row[1] for row in db.execute("PRAGMA index_list(tool_calls)")}
    assert {"ix_tool_calls_ts", "ix_tool_calls_tool"} <= indexes
