"""#3957 — telemetry rows name the model the turn ran (or was asked to run) on, and bill
the work a ``/<subagent>`` or ``/<workflow>`` short-circuit did.

1. A row whose turn reported no model usage — a turn that failed on its first call, a
   short-circuit reply — fell back to the CONFIGURED default model, even when the caller
   had requested another one. It now names the requested model (a registered connection
   prefix dropped, to match the bare ids usage rows carry).
2. A slash subagent or workflow runs in the pre-turn chain, outside the lead graph, so
   neither driver's usage accounting saw it: the row read "0 LLM calls, 0 tokens" on the
   default model. The delegation funnel's per-call usage rows now reach the row.

These drive the real drivers (``chat()`` / ``_chat_langgraph_stream`` / the A2A
executor) and the real delegation funnel (``graph.agent._run_subagent``); only the graph,
the sub-graph run inside the funnel, and the Langfuse scope are faked.
"""

from __future__ import annotations

import asyncio
import contextlib
import importlib

import pytest
from a2a.server.agent_execution import RequestContext
from a2a.server.context import ServerCallContext
from a2a.server.events.event_queue import EventQueueLegacy as EventQueue
from a2a.types import Message, Part, Role, SendMessageRequest

from a2a_impl.executor import ProtoAgentExecutor, TurnOutcome, set_terminal_hook
from graph.config import LangGraphConfig
from observability.telemetry_store import TelemetryStore
from tests._turn_driver_fakes import Invoke, ScriptedGraph

chat_mod = importlib.import_module("server.chat")
dispatch_mod = importlib.import_module("server.chat_dispatch")

_OVERRIDE = "anthropic-oauth:claude-sonnet-4-6"
_ROWS = [
    {
        "input_tokens": 120,
        "output_tokens": 30,
        "cache_read_input_tokens": 20,
        "cache_creation_input_tokens": 0,
        "cost_usd": 0.0012,
        "model": "claude-sonnet-4-6",
        "subagent_type": "researcher",
    },
    {
        "input_tokens": 200,
        "output_tokens": 50,
        "cache_read_input_tokens": 0,
        "cache_creation_input_tokens": 0,
        "cost_usd": 0.002,
        "model": "claude-sonnet-4-6",
        "subagent_type": "researcher",
    },
]


@pytest.fixture
def env(monkeypatch, tmp_path):
    """Real telemetry store, a Langfuse-like trace scope, no services."""
    from observability import tracing

    import runtime.state as rs

    store = TelemetryStore(str(tmp_path / "telemetry.db"))
    cfg = LangGraphConfig()
    cfg.model_name = "configured-default-model"
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
        "graph_config": cfg,
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

    def install(invokes=(), streams=()):
        e.graph = ScriptedGraph(streams=streams, invokes=invokes)
        monkeypatch.setattr(rs.STATE, "graph", e.graph, raising=False)
        return e.graph

    e.install = install
    return e


# ── 1. the requested model names a row with no usage ───────────────────────────────


@pytest.mark.asyncio
async def test_a_failed_override_turn_records_the_requested_model_not_the_default(env):
    class _Upstream(Exception):
        status_code = 429

    env.install([Invoke(raises=_Upstream("rate limited"))])

    await chat_mod.chat("hello", "s-fail", model=_OVERRIDE, origin="v1")

    (row,) = env.store.recent()
    assert row["state"] == "failed"
    assert row["model"] == "claude-sonnet-4-6"  # was "configured-default-model"
    assert row["trace_id"] == "trace-s-fail"


@pytest.mark.asyncio
async def test_a_short_circuit_override_turn_records_the_requested_model(env):
    env.install([])

    await chat_mod.chat("/lifecycle", "s-sc", model=_OVERRIDE, origin="api-chat")

    (row,) = env.store.recent()
    assert row["state"] == "completed" and row["model"] == "claude-sonnet-4-6"


@pytest.mark.asyncio
async def test_without_an_override_the_default_still_names_the_row(env):
    env.install([])

    await chat_mod.chat("/lifecycle", "s-def", origin="api-chat")

    (row,) = env.store.recent()
    assert row["model"] == "configured-default-model"


def test_record_turn_requested_model_rules(env):
    from server.turn_telemetry import record_turn

    record_turn(task_id="a", session_id="s", state="failed", requested_model=_OVERRIDE)
    # An unregistered prefix is not a connection — kept whole, as the caller sent it.
    record_turn(task_id="b", session_id="s", state="failed", requested_model="nope:model-x")
    # Real usage always wins over the request.
    record_turn(task_id="c", session_id="s", state="completed", models=["gpt-ran"], requested_model=_OVERRIDE)
    by_id = {r["task_id"]: r["model"] for r in env.store.recent()}
    assert by_id == {"a": "claude-sonnet-4-6", "b": "nope:model-x", "c": "gpt-ran"}


def test_a2a_row_falls_back_to_the_outcomes_requested_model(env):
    from server.a2a import _record_a2a_telemetry

    _record_a2a_telemetry(
        TurnOutcome(task_id="t1", context_id="c1", state="failed", text="", requested_model=_OVERRIDE)
    )

    (row,) = env.store.recent()
    assert row["model"] == "claude-sonnet-4-6"


def _request_context(text: str, metadata: dict | None = None) -> RequestContext:
    msg = Message(message_id="m-1", role=Role.ROLE_USER, parts=[Part(text=text)])
    if metadata:
        msg.metadata.update(metadata)
    req = SendMessageRequest(message=msg)
    return RequestContext(call_context=ServerCallContext(), request=req, task_id="t-1", context_id="c-1")


async def _execute(text: str, metadata: dict | None = None) -> list[TurnOutcome]:
    seen: list[TurnOutcome] = []
    set_terminal_hook(seen.append)
    try:
        await ProtoAgentExecutor(chat_mod._chat_langgraph_stream).execute(_request_context(text, metadata), EventQueue())
    finally:
        set_terminal_hook(None)
    return seen


@pytest.mark.asyncio
async def test_the_executor_carries_the_requested_model_onto_the_outcome(env):
    env.install([])

    (outcome,) = await _execute("/lifecycle", {"model": _OVERRIDE})

    assert outcome.state == "completed" and outcome.models == []
    assert outcome.requested_model == _OVERRIDE


# ── 2. a slash subagent / workflow bills its delegated model calls ──────────────────


@pytest.fixture
def fake_delegation(monkeypatch):
    """Route `/researcher …` and `/wf …` through the REAL delegation funnel
    (``graph.agent._run_subagent``), whose sub-graph run is faked to report two model
    calls — what ``_extract_subagent_usage`` hands the funnel for a real run."""
    import graph.agent as agent_mod

    async def _inner(**kw):
        kw["usage_sink"].extend(dict(r) for r in _ROWS)
        return f"[{kw['subagent_type']} completed]\n\nthe answer"

    monkeypatch.setattr(agent_mod, "_run_subagent_inner", _inner)

    async def _run_one(prompt: str) -> str:
        return await agent_mod._run_subagent(
            config=None,
            tool_map={},
            available_subagents="researcher",
            description="d",
            prompt=prompt,
            subagent_type="researcher",
        )

    cc = dispatch_mod._chat_commands
    monkeypatch.setattr(cc, "_parse_subagent_command", lambda m: ("researcher", "dig") if m.startswith("/researcher") else None)
    monkeypatch.setattr(cc, "_parse_workflow_command", lambda m: ("wf", {"q": 1}) if m.startswith("/wf") else None)

    async def _run_parsed_subagent(sub_type, prompt, *, session_id="", turn_model=""):
        return await _run_one(prompt)

    async def _run_parsed_workflow(name, inputs, *, on_step):
        # The workflow runner is its OWN task: the collector must reach it through the
        # task's copied context.
        await on_step({"phase": "start", "step_id": "s1", "subagent": "researcher"})
        out = await asyncio.create_task(_run_one("step"))
        await on_step({"phase": "end", "step_id": "s1", "output": out})
        return "workflow output"

    monkeypatch.setattr(cc, "_run_parsed_subagent", _run_parsed_subagent)
    monkeypatch.setattr(cc, "_run_parsed_workflow", _run_parsed_workflow)


@pytest.mark.asyncio
@pytest.mark.parametrize("command", ["/researcher dig", "/wf q=1"])
async def test_a_sync_slash_run_bills_its_delegated_model_calls(env, fake_delegation, command):
    env.install([])

    out = await chat_mod.chat(command, "s1", model=_OVERRIDE, origin="v1")

    assert out[0]["content"]
    (row,) = env.store.recent()
    assert row["state"] == "completed"
    assert row["llm_calls"] == 2  # was 0
    assert row["model"] == "claude-sonnet-4-6"
    assert row["output_tokens"] == 80
    assert row["cache_read_input_tokens"] == 20
    assert row["input_tokens"] == 300  # cache-exclusive (#3003): 320 - 20
    assert row["cost_usd"] == pytest.approx(0.0032)


@pytest.mark.asyncio
@pytest.mark.parametrize("command", ["/researcher dig", "/wf q=1"])
async def test_a_streamed_slash_run_bills_its_delegated_model_calls(env, fake_delegation, command):
    env.install([])

    (outcome,) = await _execute(command, {"model": _OVERRIDE})

    assert outcome.state == "completed"
    assert outcome.llm_calls == 2  # was 0
    assert outcome.models == ["claude-sonnet-4-6"]
    assert outcome.usage["input_tokens"] == 320 and outcome.usage["output_tokens"] == 80
    assert outcome.cost_usd == pytest.approx(0.0032)
    # Delegated calls never count toward the lead thread's context-window fill.
    assert outcome.context_tokens == 0


def test_the_collector_is_a_noop_unless_bound():
    """The collector is bound only around a short-circuit run: a ``task`` delegation
    inside the lead graph already bills through its custom usage events, and with no
    collector bound the funnel's ``note`` must do nothing (no double billing)."""
    from graph import delegation_usage

    delegation_usage.note(list(_ROWS))  # nothing bound → a no-op, never an error
    with delegation_usage.collect() as rows:
        pass
    assert rows == []


@pytest.fixture
def failing_delegation(fake_delegation, monkeypatch):
    """The same slash routes, but the sub-graph run bills two calls and THEN fails."""
    import graph.agent as agent_mod

    async def _inner(**kw):
        kw["usage_sink"].extend(dict(r) for r in _ROWS)
        raise RuntimeError("upstream fell over mid-run")

    monkeypatch.setattr(agent_mod, "_run_subagent_inner", _inner)


@pytest.mark.asyncio
@pytest.mark.parametrize("command", ["/researcher dig", "/wf q=1"])
async def test_a_failed_sync_slash_run_still_bills_what_it_spent(env, failing_delegation, command):
    """CodeRabbit (chat_dispatch): the usage frames were emitted only on success, so a
    slash run that spent tokens and then failed recorded a 0-call failed row."""
    env.install([])

    out = await chat_mod.chat(command, "s1", model=_OVERRIDE, origin="v1")

    assert out[0].get("error")
    (row,) = env.store.recent()
    assert row["state"] == "failed"
    assert row["llm_calls"] == 2 and row["output_tokens"] == 80


@pytest.mark.asyncio
@pytest.mark.parametrize("command", ["/researcher dig", "/wf q=1"])
async def test_a_failed_streamed_slash_run_still_bills_what_it_spent(env, failing_delegation, command):
    env.install([])

    (outcome,) = await _execute(command, {"model": _OVERRIDE})

    assert outcome.state == "failed"
    assert outcome.llm_calls == 2 and outcome.usage["output_tokens"] == 80
