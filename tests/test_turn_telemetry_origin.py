"""Each turn's telemetry row records what triggered it (ADR 0135 D12).

The spend cap meters unattended turns from the ``turns`` table, so the row must carry the
origin: the A2A request's ``metadata.origin`` on the streaming driver, the surface name
on the non-streaming one. These drive the real executor and ``chat()`` into a real store;
only the graph and the Langfuse scope are faked.
"""

from __future__ import annotations

import contextlib
import importlib
import sqlite3

import pytest
from a2a.server.agent_execution import RequestContext
from a2a.server.context import ServerCallContext
from a2a.server.events.event_queue import EventQueueLegacy as EventQueue
from a2a.types import Message, Part, Role, SendMessageRequest

from a2a_impl.executor import ProtoAgentExecutor, TurnOutcome, set_terminal_hook
from graph.config import LangGraphConfig
from observability.telemetry_store import TelemetryStore
from tests._turn_driver_fakes import ScriptedGraph

chat_mod = importlib.import_module("server.chat")


@pytest.fixture
def store(monkeypatch, tmp_path):
    """A real telemetry store on STATE, a scripted graph, no services."""
    from observability import tracing

    import runtime.state as rs

    store = TelemetryStore(str(tmp_path / "telemetry.db"))
    for attr, val in {
        "telemetry_store": store,
        "goal_controller": None,
        "background_mgr": None,
        "watch_controller": None,
        "scheduler": None,
        "graph_auth_error": None,
        "thread_id_resolver": None,
        "checkpointer": object(),
        "knowledge_store": None,
        "graph_config": LangGraphConfig(),
        "graph": ScriptedGraph(streams=(), invokes=()),
    }.items():
        monkeypatch.setattr(rs.STATE, attr, val, raising=False)

    @contextlib.asynccontextmanager
    async def trace_session(session_id, name="agent-session", metadata=None, input=None, incognito=False):
        yield None

    monkeypatch.setattr(tracing, "trace_session", trace_session)
    monkeypatch.setattr(tracing, "flush", lambda: None)
    monkeypatch.setattr(tracing, "set_session_output", lambda out: None)
    return store


async def _a2a_turn(metadata: dict) -> None:
    """One A2A turn through the real executor, recorded by the server's terminal hook."""
    from server.a2a import _record_a2a_telemetry

    msg = Message(message_id="m-1", role=Role.ROLE_USER, parts=[Part(text="/lifecycle")])
    msg.metadata.update(metadata)
    ctx = RequestContext(
        call_context=ServerCallContext(), request=SendMessageRequest(message=msg), task_id="t-1", context_id="c-1"
    )
    set_terminal_hook(_record_a2a_telemetry)
    try:
        await ProtoAgentExecutor(chat_mod._chat_langgraph_stream).execute(ctx, EventQueue())
    finally:
        set_terminal_hook(None)


@pytest.mark.asyncio
@pytest.mark.parametrize("metadata, origin", [({"origin": "scheduler"}, "scheduler"), ({}, "")])
async def test_an_a2a_turn_records_the_requests_origin(store, metadata, origin):
    # A scheduled fire names itself; a live operator chat turn carries no origin and stores "".
    await _a2a_turn(metadata)

    (row,) = store.recent()
    assert row["task_id"] == "t-1" and row["origin"] == origin


@pytest.mark.asyncio
async def test_a_non_streaming_turn_records_its_surface(store):
    await chat_mod.chat("/lifecycle", "s-1", origin="v1")

    (row,) = store.recent()
    assert row["origin"] == "v1"


def test_origin_is_optional_for_other_writers(store):
    from server.a2a import _record_a2a_telemetry
    from server.turn_telemetry import record_turn

    record_turn(task_id="plain", session_id="s", state="completed")  # a subagent / coder row
    store.record({"task_id": "direct", "state": "completed"})  # a caller that builds its own row
    _record_a2a_telemetry(TurnOutcome(task_id="hook", context_id="c", state="completed", text="", origin="watch"))

    assert {r["task_id"]: r["origin"] for r in store.recent()} == {"plain": "", "direct": "", "hook": "watch"}


# The columns every store has had since before the guarded migrations began.
_OLD_COLS = (
    "session_id TEXT, state TEXT, success INTEGER, model TEXT, input_tokens INTEGER, "
    "output_tokens INTEGER, total_tokens INTEGER, cache_read_input_tokens INTEGER, "
    "cache_creation_input_tokens INTEGER, cost_usd REAL, duration_ms INTEGER, llm_calls INTEGER, "
    "tool_calls INTEGER, created_at TEXT, ended_at TEXT"
)


@pytest.mark.parametrize(
    "shape",
    [
        pytest.param(f"row_id INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT, {_OLD_COLS}", id="old"),
        # `task_id TEXT PRIMARY KEY` cannot be altered, so that store is rebuilt.
        pytest.param(f"task_id TEXT PRIMARY KEY, {_OLD_COLS}", id="rebuilt"),
    ],
)
def test_an_existing_store_gains_the_column_and_old_rows_read_empty(tmp_path, shape):
    path = str(tmp_path / "old.db")
    db = sqlite3.connect(path)
    db.execute(f"CREATE TABLE turns ({shape})")
    db.execute(
        "INSERT INTO turns (task_id, state, model, input_tokens, ended_at) "
        "VALUES ('legacy-1', 'completed', 'm', 7, '2026-05-01T00:00:00+00:00')"
    )
    db.commit()
    db.close()

    store = TelemetryStore(path)
    store.record({"task_id": "new-1", "state": "completed", "origin": "scheduler", "ended_at": "2026-11-18T00:00:00"})
    TelemetryStore(path)  # reopening an already-migrated store changes nothing

    db = sqlite3.connect(path)
    assert "origin" in [r[1] for r in db.execute("PRAGMA table_info(turns)")]
    db.close()
    rows = {r["task_id"]: r for r in store.recent()}
    assert rows["legacy-1"]["origin"] == "" and rows["legacy-1"]["input_tokens"] == 7
    assert rows["new-1"]["origin"] == "scheduler"
