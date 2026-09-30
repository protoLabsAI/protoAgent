"""#3945 — the non-streaming telemetry rows and failed background jobs.

1. A ``/v1`` or ``/api/chat`` row carries the turn's trace id. The row is written by the
   ``_chat_langgraph`` wrapper AFTER the impl's ``trace_session`` scope has exited and
   reset the trace-id contextvar, so reading it there always gave "" (0/19 rows live vs
   84/84 for A2A, whose executor captures it during the stream).
2. A background job that FAILED keeps its error: the job row, the drained
   ``<task-notification>`` and the ``background.completed`` event carry it, so the agent
   reports the real cause (a 429) instead of guessing from an empty result.
3. Non-streaming statuses match A2A: a HITL park / hold is an ``input_required`` row,
   not ``completed``; a slash-command reply writes a ``completed`` row as A2A's does.

These drive the real ``chat()`` → wrapper → impl → ``record_local_turn`` → store path;
the graph (``ScriptedGraph``) and the Langfuse scope are the only fakes.
"""

from __future__ import annotations

import contextlib
import importlib

import pytest
from langchain_core.messages import AIMessage

from graph.config import LangGraphConfig
from observability.telemetry_store import TelemetryStore
from tests._turn_driver_fakes import Invoke, ScriptedGraph, set_interrupt, turn_result

chat_mod = importlib.import_module("server.chat")


@pytest.fixture
def env(monkeypatch, tmp_path):
    """Real telemetry store + a trace scope that behaves like Langfuse's: it sets the
    trace-id contextvar on entry and RESETS it on exit."""
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
    }.items():
        monkeypatch.setattr(rs.STATE, attr, val, raising=False)

    @contextlib.asynccontextmanager
    async def trace_session(session_id, name="agent-session", metadata=None, input=None, incognito=False):
        token = tracing._trace_id_ctx.set(f"trace-{session_id}")
        try:
            yield None
        finally:
            tracing._trace_id_ctx.reset(token)

    monkeypatch.setattr(tracing, "trace_session", trace_session)
    monkeypatch.setattr(tracing, "flush", lambda: None)
    monkeypatch.setattr(tracing, "set_session_output", lambda out: None)

    class Env:
        pass

    e = Env()
    e.store = store

    def install(invokes=()):
        e.graph = ScriptedGraph(invokes=invokes)
        monkeypatch.setattr(rs.STATE, "graph", e.graph, raising=False)
        return e.graph

    e.install = install
    return e


# ── 1. trace id ───────────────────────────────────────────────────────────────


@pytest.mark.asyncio
@pytest.mark.parametrize("origin", ["v1", "api-chat"])
async def test_a_non_streaming_row_carries_the_turns_trace_id(env, origin):
    env.install([Invoke(turn_result(AIMessage(content="The answer.")), usage=[("m1", 100, 20)])])

    await chat_mod.chat("hello", "s1", origin=origin)

    (row,) = env.store.recent()
    assert row["state"] == "completed"
    assert row["trace_id"] == "trace-s1"


@pytest.mark.asyncio
async def test_a_failed_non_streaming_row_carries_the_trace_id_and_stays_failed(env):
    """#3934's failed-turn row keeps its shape (and #3914's `error` key still marks it)."""

    class _Upstream(Exception):
        status_code = 429

    env.install([Invoke(raises=_Upstream("rate limited"))])

    out = await chat_mod.chat("hello", "s-err", origin="v1")

    assert out[0]["error"]["upstream_status"] == 429
    (row,) = env.store.recent()
    assert row["state"] == "failed" and row["success"] == 0
    assert row["trace_id"] == "trace-s-err"


# ── 3. statuses aligned with A2A ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_a_hitl_park_is_recorded_input_required_not_completed(env):
    env.install([Invoke(turn_result(), steps=[set_interrupt({"question": "Which env?"})], usage=[("m1", 5, 1)])])

    out = await chat_mod.chat("deploy", "s1", origin="api-chat")

    assert "Input needed" in out[0]["content"]
    (row,) = env.store.recent()
    assert row["state"] == "input_required"
    assert row["success"] is None  # neither half of the success rate, as on A2A
    assert row["input_tokens"] == 5 and row["trace_id"] == "trace-s1"


@pytest.mark.asyncio
async def test_a_hitl_hold_is_recorded_input_required(env):
    from graph import steering

    g = env.install([])
    g.pending.append({"question": "Which env?"})
    try:
        out = await chat_mod.chat("use the fast path", "s1", origin="api-chat")
    finally:
        steering.forget("s1")

    assert "Input needed first" in out[0]["content"]
    (row,) = env.store.recent()
    assert row["state"] == "input_required" and row["total_tokens"] == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("origin", ["v1", "api-chat"])
@pytest.mark.parametrize("command", ["/lifecycle", "/zzqq-no-such-command"])
async def test_a_slash_command_reply_writes_a_completed_row_like_a2a(env, origin, command):
    g = env.install([])

    out = await chat_mod.chat(command, "s1", origin=origin)

    assert out and out[0]["content"]
    assert g.invoke_calls == []  # answered before the graph
    (row,) = env.store.recent()
    assert row["state"] == "completed" and row["success"] == 1
    assert row["task_id"].startswith(f"{origin}:")
    assert row["total_tokens"] == 0 and row["trace_id"] == "trace-s1"


def test_an_empty_sink_that_completed_still_gets_no_row(env):
    """The one no-row case left: nothing reached the impl (`setup not complete`)."""
    chat_mod._record_local_turn({}, session_id="s1", origin="v1", state="completed", started=0.0)
    assert env.store.recent() == []


# ── 2. failed background job keeps its error ─────────────────────────────────


@pytest.fixture
def bg(monkeypatch, tmp_path):
    from background.manager import BackgroundManager
    from background.store import BackgroundStore

    import runtime.state as rs
    import server.a2a as a2a

    mgr = BackgroundManager(
        agent_name="a",
        invoke_url="http://127.0.0.1:7870",
        store=BackgroundStore(str(tmp_path / "background" / "jobs.db")),
        api_key="k",
        bearer_token="b",
    )
    monkeypatch.setattr(rs.STATE, "background_mgr", mgr, raising=False)
    monkeypatch.setattr(rs.STATE, "graph_config", LangGraphConfig(background_auto_resume=False), raising=False)
    monkeypatch.setattr(rs.STATE, "knowledge_store", None, raising=False)
    monkeypatch.delenv("BACKGROUND_WAKE", raising=False)
    monkeypatch.setattr(a2a, "_spawn_background_wake", lambda job: None)
    published: list = []
    monkeypatch.setattr(a2a._event_bus, "publish", lambda t, d=None, **kw: published.append((t, d)))
    jid = mgr.store.create(
        agent_name="a", origin_session="chat-42", subagent_type="researcher", description="dig", prompt="p"
    )
    return a2a, mgr, jid, published


def _outcome(job_id, *, state, text="", error=""):
    from a2a_impl.executor import TurnOutcome

    return TurnOutcome(
        task_id="t1",
        context_id=f"background:{job_id}",
        state=state,
        text=text,
        origin="background",
        trigger=job_id,
        error=error,
    )


def test_a_failed_background_job_keeps_its_error(bg):
    a2a, mgr, jid, published = bg
    err = "Error code: 429 - rate limit exceeded"

    a2a._handle_background_terminal(_outcome(jid, state="failed", error=err))

    job = mgr.store.get(jid)
    assert job.status == "failed" and job.error == err
    assert job.to_dict()["error"] == err
    ((_, event),) = [p for p in published if p[0] == "background.completed"]
    assert event["status"] == "failed" and event["error"] == err

    (msg,) = chat_mod._drain_background_messages("chat-42")
    assert f"<error>{err}</error>" in msg.content
    assert "<status>failed</status>" in msg.content


def test_a_completed_background_job_has_no_error_element(bg):
    a2a, mgr, jid, published = bg

    a2a._handle_background_terminal(_outcome(jid, state="completed", text="the report", error="ignored"))

    assert mgr.store.get(jid).error == ""
    (msg,) = chat_mod._drain_background_messages("chat-42")
    assert "<error>" not in msg.content and "the report" in msg.content


def test_the_error_column_migrates_onto_an_old_db(tmp_path):
    import sqlite3

    from background.store import BackgroundStore

    path = tmp_path / "old.db"
    db = sqlite3.connect(path)
    db.execute(
        "CREATE TABLE background_jobs (id TEXT PRIMARY KEY, agent_name TEXT NOT NULL, origin_session TEXT NOT NULL,"
        " subagent_type TEXT NOT NULL, description TEXT NOT NULL, prompt TEXT NOT NULL, status TEXT NOT NULL,"
        " result TEXT, notified INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL, completed_at TEXT)"
    )
    db.execute(
        "INSERT INTO background_jobs VALUES ('j1','a','s','r','d','p','failed','',0,'2026-01-01T00:00:00+00:00',NULL)"
    )
    db.commit()
    db.close()

    s = BackgroundStore(str(path))
    assert s.get("j1").error == ""
    jid = s.create(agent_name="a", origin_session="s", subagent_type="r", description="d", prompt="p")
    assert s.mark_complete(jid, "failed", "", error="boom") is True
    assert s.get(jid).error == "boom"
