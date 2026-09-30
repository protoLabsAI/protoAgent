"""The turn's model override reaches every subagent dispatch path (#3955).

Follow-up to #3944/#3948, which gave subagent runs ONE precedence — the subagent's pin >
the turn's model override > ``routing.aux_model`` > the main model — but only where the
override was already in hand (``metadata.model`` on the streaming driver, ``state["model"]``
inside the lead graph). Three paths still dropped it:

* the non-streaming driver (``/api/chat``, ``/v1``) called ``_pre_turn_dispatch`` with no
  metadata, so ``/<subagent>`` ran on the default (live: the default codex model 429'd;
  ``/v1`` answered HTTP 500);
* ``/<workflow>`` steps on EVERY driver: ``graph.sdk.run_subagent`` called
  ``run_manual_subagent`` without ``turn_model``;
* ``graph.sdk.spawn_background`` (a plugin tool spawning mid-turn) fired with no model.

The override now rides ``_PreTurn.turn_model`` into the pre-turn chain and a
``turn_model_scope`` contextvar (bound by both drivers for the whole turn) into the SDK.
"""

from __future__ import annotations

import importlib

import pytest
from langchain_core.messages import AIMessage

import graph.agent as agent_mod
import graph.sdk as sdk
import server.chat_commands as chat_commands
import server.chat_dispatch as chat_dispatch
from graph.config import LangGraphConfig
from graph.subagent_model import current_turn_model, turn_model_scope
from runtime.state import STATE
from tests._turn_driver_fakes import ScriptedGraph, TraceSpy, turn_result
from tests.test_background import _drain_fire_tasks, _manager
from tests.test_subagent_model_precedence_3944 import OVERRIDE, PIN, fired, stub  # noqa: F401 — fixtures

chat_mod = importlib.import_module("server.chat")  # `server.chat` the attribute is the chat() function

pytestmark = pytest.mark.asyncio


@pytest.fixture
def env(monkeypatch, stub):  # noqa: F811 — the imported fixture
    """Both drivers runnable with a scripted graph; a registry subagent ``stub`` whose
    run records the model it was built on; ``/stub …`` parses as a subagent command."""
    from observability import metrics

    import server.turn_telemetry as turn_telemetry

    TraceSpy().install(monkeypatch)
    monkeypatch.setattr(turn_telemetry, "record_local_turn", lambda *a, **k: None)
    monkeypatch.setattr(metrics, "record_overflow_recovery", lambda: None)
    for attr, val in {
        "goal_controller": None,
        "background_mgr": None,
        "watch_controller": None,
        "scheduler": None,
        "graph_auth_error": None,
        "thread_id_resolver": None,
        "checkpointer": object(),
        "knowledge_store": None,
        "graph_config": LangGraphConfig(),
        "plugin_chat_commands": {},
        "plugin_tools": [],
        "mcp_tools": [],
        "workflow_run": None,
    }.items():
        monkeypatch.setattr(STATE, attr, val, raising=False)
    monkeypatch.setattr(STATE, "graph", ScriptedGraph(), raising=False)
    monkeypatch.setattr(chat_commands, "_parse_workflow_command", lambda m: None)
    monkeypatch.setattr(
        chat_commands, "_parse_subagent_command", lambda m: ("stub", "dig") if m.startswith("/stub") else None
    )
    monkeypatch.setattr(agent_mod, "get_all_tools", lambda *_a, **_k: [])
    return stub


@pytest.fixture
def workflow(env, monkeypatch):
    """``/wf`` parses as a workflow whose one step runs ``stub`` through the SDK — the
    exact call the workflows plugin's engine makes per step."""

    async def _workflow_run(name, inputs, on_step=None):
        out = await sdk.run_subagent("stub", "p", description=f"workflow {name}:s1")
        return {"output": out, "failed": []}

    monkeypatch.setattr(STATE, "workflow_run", _workflow_run, raising=False)
    monkeypatch.setattr(chat_commands, "_parse_workflow_command", lambda m: ("wf", {}) if m.startswith("/wf") else None)
    return env


# ── /<subagent> on the non-streaming driver (/api/chat, /v1) ───────────────────


async def test_sync_slash_subagent_runs_on_the_turn_override(env):
    out = await chat_mod.chat("/stub dig", "s1", model=OVERRIDE, origin="v1")
    assert out[0]["content"] and "error" not in out[0], out
    assert env.built == [OVERRIDE]


async def test_sync_slash_subagent_pinned_model_wins_over_the_override(env):
    env.register(PIN)
    await chat_mod.chat("/stub dig", "s1", model=OVERRIDE)
    assert env.built == [PIN]


async def test_sync_slash_subagent_without_override_is_unchanged(env):
    await chat_mod.chat("/stub dig", "s1")
    assert env.built == [None]


# ── /<workflow> steps, on both drivers ─────────────────────────────────────────


async def test_sync_workflow_step_runs_on_the_turn_override(workflow):
    await chat_mod.chat("/wf go", "s1", model=OVERRIDE, origin="v1")
    assert workflow.built == [OVERRIDE]


async def test_stream_workflow_step_runs_on_the_turn_override(workflow):
    pre = chat_dispatch._PreTurn("/wf go")
    frames = [f async for f in chat_dispatch._pre_turn_dispatch(pre, "s-wf", {"model": OVERRIDE})]
    assert pre.handled and frames[-1][0] == "done", frames
    assert workflow.built == [OVERRIDE]


async def test_workflow_step_pinned_model_wins_over_the_override(workflow):
    workflow.register(PIN)
    await chat_mod.chat("/wf go", "s1", model=OVERRIDE)
    assert workflow.built == [PIN]


async def test_workflow_step_without_override_is_unchanged(workflow):
    await chat_mod.chat("/wf go", "s1")
    assert workflow.built == [None]


# ── the scope both drivers bind around the lead graph run ─────────────────────


def _record_scope(seen: list):
    graph = STATE.graph
    graph.on_call = lambda _g, _cfg: seen.append(current_turn_model())
    return graph


async def test_sync_driver_binds_the_override_for_the_graph_run(env):
    seen: list = []
    _record_scope(seen).invokes.append(turn_result(AIMessage(content="ok")))
    await chat_mod.chat("hi", "s1", model=f"  {OVERRIDE} ")
    assert seen == [OVERRIDE]
    assert current_turn_model() == ""  # unbound once the turn is over


async def test_stream_driver_binds_the_override_for_the_graph_run(env):
    from tests._turn_driver_fakes import text

    seen: list = []
    _record_scope(seen).streams.append([text("r1", "ok")])
    frames = [f async for f in chat_mod._chat_langgraph_stream("hi", "s1", request_metadata={"model": OVERRIDE})]
    assert frames[-1][0] == "done", frames
    assert seen == [OVERRIDE]


# ── the SDK seam itself ────────────────────────────────────────────────────────


async def test_sdk_run_subagent_reads_the_turn_scope(env):
    with turn_model_scope(OVERRIDE):
        await sdk.run_subagent("stub", "p", description="d")
    await sdk.run_subagent("stub", "p", description="d")  # outside a turn: pin > aux > main
    await sdk.run_subagent("stub", "p", description="d", turn_model="explicit/model")
    assert env.built == [OVERRIDE, None, "explicit/model"]


async def test_sdk_spawn_background_carries_the_turn_override(tmp_path, env, fired, monkeypatch):  # noqa: F811
    mgr = _manager(tmp_path)
    monkeypatch.setattr(STATE, "background_mgr", mgr, raising=False)
    with turn_model_scope(OVERRIDE):
        res = await sdk.spawn_background("p", subagent_type="stub", origin_session="s1")
    assert res["ok"], res
    await _drain_fire_tasks(mgr)
    assert fired()["model"] == OVERRIDE
