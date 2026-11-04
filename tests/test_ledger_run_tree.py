"""A finished run can be rebuilt from durable storage alone (#4726).

Before this, the parent/child nesting of delegations lived only in the live registry
(``graph/ledger.py::_LIVE``) and was gone the moment an edge closed, and several producers
wrote rows with no session, task or parent id at all. These tests assert the DURABLE ROWS,
after the live registry has let go of them, through the real funnels: the A2A executor, the
subagent funnel, the background manager, the workflow runner and the delegate registry.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from graph import ledger
from observability.ledger_store import LedgerStore

pytestmark = pytest.mark.asyncio


@pytest.fixture
def ledger_db(tmp_path, monkeypatch):
    import runtime.state as rs

    store = LedgerStore(str(tmp_path / "ledger.db"))
    monkeypatch.setattr(rs.STATE, "ledger_store", store, raising=False)
    monkeypatch.setattr(rs.STATE, "graph_config", None, raising=False)
    monkeypatch.setattr(ledger, "_LIVE", {})
    monkeypatch.setattr(ledger, "_LIVE_BY_TASK", {})
    return store


def _tree(rows: list[dict]) -> dict[str, list[str]]:
    """parent edge_uuid -> [child to_name], rebuilt from the rows only."""
    out: dict[str, list[str]] = {}
    for r in rows:
        out.setdefault(r["parent_uuid"] or "", []).append(r["to_name"])
    return out


# --- the ledger itself -------------------------------------------------------------------


async def test_a_nested_dispatch_keeps_its_parent_after_both_edges_close(ledger_db):
    with ledger.run_scope(session_id="s1", run_id="turn-1"):
        with ledger.dispatch(to_kind="subagent", to_name="planner"):
            with ledger.dispatch(to_kind="acp", to_name="coder"):
                pass

    assert ledger.live_edges() == []  # the live registry has let go of both
    rows = ledger_db.run("turn-1")
    by_name = {r["to_name"]: r for r in rows}
    assert set(by_name) == {"planner", "coder"}
    assert by_name["coder"]["parent_uuid"] == by_name["planner"]["edge_uuid"]
    assert by_name["planner"]["parent_uuid"] == ""
    assert {r["session_id"] for r in rows} == {"s1"}
    assert all(len(r["edge_uuid"]) == 16 for r in rows)


async def test_an_edge_outside_any_run_is_its_own_root(ledger_db):
    with ledger.dispatch(to_kind="acp", to_name="coder"):
        with ledger.dispatch(to_kind="subagent", to_name="helper"):
            pass

    rows = ledger_db.recent()
    root = next(r for r in rows if r["to_name"] == "coder")
    child = next(r for r in rows if r["to_name"] == "helper")
    assert root["run_id"] == root["edge_uuid"]
    assert child["run_id"] == root["edge_uuid"]


async def test_a_nested_scope_inherits_the_fields_it_does_not_set():
    with ledger.run_scope(session_id="s1", run_id="turn-1"):
        with ledger.run_scope(parent_uuid="p"):
            assert ledger.current_run_id() == "turn-1"
        assert ledger.current_run_id() == "turn-1"
    assert ledger.current_run_id() == ""


async def test_a_task_id_learned_mid_dispatch_lands_on_the_row(ledger_db):
    with ledger.dispatch(to_kind="acp", to_name="coder"):
        ledger.note_task_id(ledger.current_edge_id(), "coder:coder:abc")

    assert ledger_db.recent()[0]["task_id"] == "coder:coder:abc"


async def test_a_block_can_report_failure_without_raising(ledger_db):
    with ledger.dispatch(to_kind="workflow", to_name="wf") as edge:
        edge.outcome, edge.error = "failed", "failed steps: b"

    row = ledger_db.recent()[0]
    assert (row["outcome"], row["error"]) == ("failed", "failed steps: b")


# --- funnel 1: in-process subagents --------------------------------------------------------


async def test_a_task_call_with_no_session_in_reach_records_the_turns_session(ledger_db, monkeypatch):
    # The `task` / `task_batch` call sites run inside a tool body, where the session isn't
    # in reach, so they pass session_id="" (agent.py). The turn's scope fills it in.
    import graph.agent as agent

    async def _inner(**_kw):
        return "done"

    monkeypatch.setattr(agent, "_run_subagent_inner", _inner)
    with ledger.run_scope(session_id="chat-7", run_id="task-7"):
        await agent._run_subagent(
            config=None,
            tool_map={},
            available_subagents="researcher",
            description="dig",
            prompt="go",
            subagent_type="researcher",
            parent_task_id="call_1",
        )

    row = ledger_db.recent()[0]
    assert (row["session_id"], row["run_id"], row["parent_task_id"]) == ("chat-7", "task-7", "call_1")


# --- the A2A executor ------------------------------------------------------------------------


def _request_context(task_id: str, context_id: str):
    from a2a.server.agent_execution import RequestContext
    from a2a.server.context import ServerCallContext
    from a2a.types import Message, Part, Role, SendMessageRequest

    req = SendMessageRequest(message=Message(message_id="m-1", role=Role.ROLE_USER, parts=[Part(text="hi")]))
    return RequestContext(call_context=ServerCallContext(), request=req, task_id=task_id, context_id=context_id)


@pytest.mark.parametrize("stall", [0.0, 30.0], ids=["guard-off", "guard-on"])
async def test_a_turn_scopes_every_edge_it_opens_as_one_run(ledger_db, stall):
    from a2a.server.events.event_queue import EventQueueLegacy as EventQueue

    from a2a_impl.executor import ProtoAgentExecutor, set_terminal_hook

    seen_run: list[str] = []
    set_terminal_hook(lambda _o: seen_run.append(ledger.current_run_id()))

    async def stream(text, context_id, **kwargs):
        with ledger.dispatch(to_kind="subagent", to_name="researcher"):
            with ledger.dispatch(to_kind="acp", to_name="coder"):
                pass
        yield ("done", "fine")

    try:
        await ProtoAgentExecutor(stream, stall_timeout_provider=lambda: stall).execute(
            _request_context("task-1", "chat-1"), EventQueue()
        )
    finally:
        set_terminal_hook(None)

    rows = ledger_db.run("task-1")
    assert {r["to_name"] for r in rows} == {"researcher", "coder"}
    assert {r["session_id"] for r in rows} == {"chat-1"}
    assert _tree(rows)[""] == ["researcher"]
    # The terminal hook (where record_turn runs) sees the run, so the turn row carries it.
    assert seen_run == ["task-1"]
    # ...and the scope is gone once the turn is.
    assert ledger.current_run_id() == ""


async def test_a_background_jobs_turn_joins_the_run_that_spawned_it(ledger_db, tmp_path, monkeypatch):
    from a2a.server.events.event_queue import EventQueueLegacy as EventQueue

    from a2a_impl.executor import ProtoAgentExecutor
    from background.manager import BackgroundManager
    from background.store import BackgroundStore

    mgr = BackgroundManager(
        agent_name="a",
        invoke_url="http://127.0.0.1:7870",
        store=BackgroundStore(str(Path(tmp_path) / "bg.db")),
        api_key="k",
        bearer_token="b",
    )

    async def _fire(*_a, **_kw):
        return None

    monkeypatch.setattr(mgr, "_fire", _fire)
    with ledger.run_scope(session_id="chat-1", run_id="task-1"):
        job_id = await mgr.spawn(
            origin_session="chat-1", subagent_type="researcher", description="dig later", prompt="go"
        )

    # A restart empties the live registry; the durable row still links the job.
    ledger._LIVE.clear()
    ledger._LIVE_BY_TASK.clear()

    async def job_turn(text, context_id, **kwargs):
        with ledger.dispatch(to_kind="subagent", to_name="verifier"):
            pass
        yield ("done", "ok")

    await ProtoAgentExecutor(job_turn).execute(_request_context("job-task", f"background:{job_id}"), EventQueue())

    rows = ledger_db.run("task-1")
    job_edge = next(r for r in rows if r["task_id"] == job_id)
    verifier = next(r for r in rows if r["to_name"] == "verifier")
    assert verifier["parent_uuid"] == job_edge["edge_uuid"]
    assert verifier["session_id"] == f"background:{job_id}"


# --- the workflow runner -------------------------------------------------------------------


async def test_a_workflow_run_is_an_edge_its_steps_nest_under(ledger_db, tmp_path, monkeypatch):
    import plugins.workflows as wf
    from plugins.workflows.run_state import WorkflowRunStore

    async def _step(name, subagent_type, prompt, step_id):
        if step_id == "b":
            raise RuntimeError("step b broke")
        with ledger.dispatch(to_kind="subagent", to_name=subagent_type, what=f"workflow {name}:{step_id}"):
            return "out"

    monkeypatch.setattr(wf, "_traced_step", _step)
    recipe = {
        "name": "two-step",
        "steps": [
            {"id": "a", "subagent": "researcher", "prompt": "x"},
            {"id": "b", "subagent": "verifier", "prompt": "y", "depends_on": ["a"]},
        ],
    }
    store = WorkflowRunStore(Path(tmp_path) / ".runs")
    run_id = store.start("two-step", {}, steps=recipe["steps"])
    with ledger.run_scope(session_id="chat-1", run_id="task-1"):
        result = await wf._run_prepared(recipe, "two-step", {}, store, run_id)

    assert result["failed"] == ["b"]
    rows = ledger_db.run("task-1")
    run_edge = next(r for r in rows if r["to_kind"] == "workflow")
    assert run_edge["task_id"] == run_id  # the run file joins the ledger here
    assert run_edge["outcome"] == "failed"
    assert "b" in run_edge["error"]
    step = next(r for r in rows if r["to_name"] == "researcher")
    assert step["parent_uuid"] == run_edge["edge_uuid"]


# --- the delegate registry -------------------------------------------------------------------


async def test_a_delegate_dispatch_inside_a_turn_records_the_turns_run(ledger_db, monkeypatch):
    from plugins.delegates.adapters import ADAPTERS
    from plugins.delegates.registry import DelegateRegistry

    async def _dispatch(d, query, *, timeout=None, item_id=None, resume_task_id=None):
        # The adapter learns the delegate's task id mid-dispatch (a peer's task, a coder's
        # telemetry key) and records it on the edge it is running inside.
        ledger.note_task_id(ledger.current_edge_id(), "peer-task-9")
        return "done"

    monkeypatch.setattr(ADAPTERS["acp"], "dispatch", _dispatch)
    reg = DelegateRegistry([{"name": "coder", "type": "acp", "command": "proto", "workdir": "/tmp"}])
    with ledger.run_scope(session_id="chat-1", run_id="task-1"):
        await reg.dispatch("coder", "implement the thing")

    row = ledger_db.run("task-1")[0]
    assert (row["to_name"], row["session_id"], row["task_id"]) == ("coder", "chat-1", "peer-task-9")


# --- the stores ------------------------------------------------------------------------------


async def test_a_ledger_created_before_the_tree_columns_gains_them(tmp_path):
    path = tmp_path / "old.db"
    db = sqlite3.connect(path)
    db.execute(
        "CREATE TABLE delegations (edge_id INTEGER PRIMARY KEY AUTOINCREMENT, at TEXT, from_agent TEXT,"
        " to_kind TEXT, to_name TEXT, to_instance TEXT, what TEXT, session_id TEXT, parent_task_id TEXT,"
        " task_id TEXT, outcome TEXT, error TEXT, duration_ms INTEGER DEFAULT 0, cost_usd REAL, origin TEXT)"
    )
    db.execute("INSERT INTO delegations (to_kind, to_name, outcome) VALUES ('subagent', 'old', 'ok')")
    db.commit()
    db.close()

    store = LedgerStore(str(path))
    store.record(from_agent="a", to_kind="subagent", to_name="new", edge_uuid="e1", run_id="r1")
    assert [r["to_name"] for r in store.run("r1")] == ["new"]
    assert {r["to_name"]: r["run_id"] for r in store.recent()} == {"new": "r1", "old": None}


async def test_a_turn_row_records_the_run_it_ran_in(tmp_path, monkeypatch):
    import runtime.state as rs
    from observability.telemetry_store import TelemetryStore
    from server.turn_telemetry import record_turn

    store = TelemetryStore(str(tmp_path / "t.db"))
    monkeypatch.setattr(rs.STATE, "telemetry_store", store, raising=False)
    with ledger.run_scope(session_id="background:bg-1", run_id="task-1"):
        record_turn(task_id="job-task", session_id="background:bg-1", state="completed", publish_usage_event=False)

    assert [r["task_id"] for r in store.by_run("task-1")] == ["job-task"]


async def test_a_pre_3001_turns_store_migrates_with_the_run_column(tmp_path):
    # The #3001 rebuild copies every column the old table has into a hardcoded new
    # shape, so a column missing from that shape breaks the migration outright.
    from observability.telemetry_store import TelemetryStore

    path = tmp_path / "old.db"
    db = sqlite3.connect(path)
    db.execute("CREATE TABLE turns (task_id TEXT PRIMARY KEY, session_id TEXT, state TEXT, ended_at TEXT)")
    db.execute("INSERT INTO turns VALUES ('t1', 's1', 'completed', '2026-01-01')")
    db.commit()
    db.close()

    store = TelemetryStore(str(path))
    store.record({"task_id": "t2", "session_id": "s", "state": "completed", "run_id": "r9"})
    assert [r["task_id"] for r in store.by_run("r9")] == ["t2"]
    assert {r["task_id"] for r in store.recent()} == {"t1", "t2"}


# --- the API ---------------------------------------------------------------------------------


async def test_the_run_route_resolves_a_job_id_to_the_run_it_belongs_to(ledger_db, tmp_path, monkeypatch):
    import runtime.state as rs
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from observability.telemetry_store import TelemetryStore
    from operator_api import telemetry_routes

    tstore = TelemetryStore(str(tmp_path / "t.db"))
    monkeypatch.setattr(rs.STATE, "telemetry_store", tstore, raising=False)
    ledger_db.record(from_agent="a", to_kind="subagent", to_name="r", task_id="bg-1", edge_uuid="e1", run_id="task-1")
    tstore.record({"task_id": "task-1", "session_id": "s", "state": "completed", "run_id": "task-1"})

    app = FastAPI()
    telemetry_routes.register_telemetry_routes(app)
    body = TestClient(app).get("/api/runs/bg-1").json()

    assert body["run_id"] == "task-1" and body["resolved_from"] == "bg-1"
    assert [e["edge_uuid"] for e in body["edges"]] == ["e1"]
    assert [t["task_id"] for t in body["turns"]] == ["task-1"]


def _run_client():
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from operator_api import telemetry_routes

    app = FastAPI()
    telemetry_routes.register_telemetry_routes(app)
    return TestClient(app)


async def test_the_run_route_resolves_an_edge_uuid_deep_inside_a_run(ledger_db, tmp_path, monkeypatch):
    # The run inspector links to one edge (the org chart's history rows carry an edge, not
    # its run), so a non-root edge's uuid has to find the run it belongs to.
    import runtime.state as rs

    monkeypatch.setattr(rs.STATE, "telemetry_store", None, raising=False)
    ledger_db.record(from_agent="a", to_kind="subagent", to_name="r", edge_uuid="e1", run_id="task-1")
    ledger_db.record(from_agent="a", to_kind="acp", to_name="c", edge_uuid="e2", parent_uuid="e1", run_id="task-1")

    body = _run_client().get("/api/runs/e2").json()

    assert body["run_id"] == "task-1" and body["resolved_from"] == "e2"
    assert [e["edge_uuid"] for e in body["edges"]] == ["e1", "e2"]
    assert "langfuse_trace_url_template" in body and "tracing_enabled" in body


async def test_the_run_route_resolves_an_edge_still_in_flight(ledger_db, monkeypatch):
    # A live edge has no durable row until it closes, so its id resolves from the live
    # registry, and the run's own id answers with it in `live` and no resolution.
    import runtime.state as rs

    monkeypatch.setattr(rs.STATE, "telemetry_store", None, raising=False)
    with ledger.run_scope(session_id="s1", run_id="turn-9"):
        edge_id = ledger.open_edge(to_kind="subagent", to_name="planner", task_id="job-9")
    try:
        client = _run_client()
        by_edge = client.get(f"/api/runs/{edge_id}").json()
        by_job = client.get("/api/runs/job-9").json()
        by_run = client.get("/api/runs/turn-9").json()
    finally:
        ledger.close_edge(edge_id, outcome="ok")

    assert (by_edge["run_id"], by_edge["resolved_from"]) == ("turn-9", edge_id)
    assert (by_job["run_id"], by_job["resolved_from"]) == ("turn-9", "job-9")
    assert by_run["resolved_from"] == "" and [e["id"] for e in by_run["live"]] == [edge_id]


async def test_an_unknown_run_id_answers_empty_not_an_error(ledger_db, monkeypatch):
    import runtime.state as rs

    monkeypatch.setattr(rs.STATE, "telemetry_store", None, raising=False)
    body = _run_client().get("/api/runs/nope").json()
    assert (body["run_id"], body["resolved_from"], body["edges"], body["turns"], body["live"]) == ("nope", "", [], [], [])
