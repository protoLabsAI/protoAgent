"""One model precedence for every subagent dispatch path (#3944).

The subagent's own pinned model (``subagents.<name>.model``) > the turn's model
override (``metadata.model`` → ``state["model"]``) > ``routing.aux_model`` > the main
model. Before the fix the override never reached a subagent: an in-graph ``task()``
delegation and a ``/<subagent>`` slash run resolved pin → aux → main, and a background
job's detached turn carried no model at all — it ran on the default even with the
subagent pinned (seen live: the default rate-limited, job ``bg-88de7df21942`` 429'd
with ``subagents.researcher.model`` pinned to Sonnet).
"""

from __future__ import annotations

import types

import pytest
from langchain_core.messages import AIMessage

import graph.agent as agent_mod
import server.chat_commands as chat_commands
import server.chat_dispatch as chat_dispatch
from graph.config import LangGraphConfig
from graph.subagent_model import resolve_subagent_model
from graph.subagents.config import SUBAGENT_REGISTRY, SubagentConfig
from runtime.state import STATE
from tests.test_background import _drain_fire_tasks, _FakeClient, _FakeResponse, _manager

OVERRIDE = "anthropic-oauth:claude-sonnet-4-6"
PIN = "protolabs/pinned"


class _FakeSubagent:
    async def astream(self, _inputs, config=None, stream_mode=None):
        yield {"messages": [AIMessage(content="done")]}


@pytest.fixture
def stub(monkeypatch):
    """A registry subagent ``stub`` whose run records the model it was built on."""
    built: list = []

    def _register(model: str = ""):
        cfg = SubagentConfig(name="stub", description="d", system_prompt="p", tools=["current_time"], model=model)
        monkeypatch.setitem(SUBAGENT_REGISTRY, "stub", cfg)

    def _create_llm(_cfg, *, model_name=None, **_kw):
        built.append(model_name)
        return object()

    _register()
    monkeypatch.setattr(agent_mod, "_subagent_tools", lambda *_a, **_k: [object()])
    monkeypatch.setattr(agent_mod, "create_llm", _create_llm)
    monkeypatch.setattr(agent_mod, "create_agent", lambda **_k: _FakeSubagent())
    monkeypatch.setattr(agent_mod, "build_subagent_prompt", lambda *_a, **_k: "sys")
    return types.SimpleNamespace(built=built, register=_register)


# ── the resolver ───────────────────────────────────────────────────────────────


def test_precedence_pin_then_override_then_aux_then_main(stub):
    cfg = LangGraphConfig()
    cfg.aux_model = "protolabs/fast"
    assert resolve_subagent_model(cfg, "stub", OVERRIDE) == OVERRIDE  # no pin → override
    assert resolve_subagent_model(cfg, "stub", "  ") == "protolabs/fast"  # blank override → aux
    assert resolve_subagent_model(LangGraphConfig(), "stub", "") is None  # nothing → main
    stub.register(PIN)
    assert resolve_subagent_model(cfg, "stub", OVERRIDE) == PIN  # the pin wins
    # The background form stops after the override: no pin, no override → the default.
    stub.register("")
    assert resolve_subagent_model(cfg, "stub", "", include_default=False) is None


# ── in-graph task() ────────────────────────────────────────────────────────────


async def _task(state: dict, **args):
    tools = {t.name: t for t in agent_mod._build_task_tools(LangGraphConfig(), [])}
    return await tools["task"].ainvoke(
        {
            "name": "task",
            "args": {"description": "d", "prompt": "p", "subagent_type": "stub", "state": state, **args},
            "id": "c1",
            "type": "tool_call",
        }
    )


async def test_task_delegation_runs_on_the_turn_override(stub):
    await _task({"model": OVERRIDE, "session_id": "s1"})
    assert stub.built[0] == OVERRIDE


async def test_task_delegation_pinned_model_wins_over_the_override(stub):
    stub.register(PIN)
    await _task({"model": OVERRIDE, "session_id": "s1"})
    assert stub.built[0] == PIN


async def test_task_delegation_without_override_is_unchanged(stub):
    await _task({"session_id": "s1"})
    assert stub.built[0] is None  # no pin, no aux, no override → the main model


async def test_task_batch_delegations_run_on_the_turn_override(stub):
    tools = {t.name: t for t in agent_mod._build_task_tools(LangGraphConfig(), [])}
    await tools["task_batch"].ainvoke(
        {
            "name": "task_batch",
            "args": {
                "tasks": [{"description": "a", "prompt": "p", "subagent_type": "stub"}],
                "state": {"model": OVERRIDE, "session_id": "s1"},
            },
            "id": "tb1",
            "type": "tool_call",
        }
    )
    assert stub.built[0] == OVERRIDE


# ── /<subagent> slash runs ─────────────────────────────────────────────────────


@pytest.fixture
def slash(monkeypatch, stub):
    monkeypatch.setattr(STATE, "goal_controller", None, raising=False)
    monkeypatch.setattr(STATE, "plugin_chat_commands", {}, raising=False)
    monkeypatch.setattr(STATE, "graph_config", LangGraphConfig(), raising=False)
    monkeypatch.setattr(chat_commands, "_parse_workflow_command", lambda m: None)
    monkeypatch.setattr(chat_commands, "_parse_subagent_command", lambda m: ("stub", "dig"))
    monkeypatch.setattr(agent_mod, "get_all_tools", lambda *_a, **_k: [])
    return stub


async def _slash(metadata):
    pre = chat_dispatch._PreTurn("/stub dig")
    frames = [f async for f in chat_dispatch._pre_turn_dispatch(pre, "s-slash", metadata)]
    assert pre.handled and frames[-1][0] == "done", frames
    return frames


async def test_slash_subagent_runs_on_the_turn_override(slash):
    await _slash({"model": OVERRIDE})
    assert slash.built[0] == OVERRIDE


async def test_slash_subagent_pinned_model_wins_over_the_override(slash):
    slash.register(PIN)
    await _slash({"model": OVERRIDE})
    assert slash.built[0] == PIN


async def test_slash_subagent_without_override_is_unchanged(slash):
    await _slash(None)
    assert slash.built[0] is None


# ── background jobs ────────────────────────────────────────────────────────────


@pytest.fixture
def fired(monkeypatch):
    import httpx

    _FakeClient.captured = {}
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: _FakeClient(_FakeResponse(200)))

    def _metadata() -> dict:
        return _FakeClient.captured["json"]["params"]["message"]["metadata"]

    return _metadata


async def _spawn(tmp_path, **kw):
    mgr = _manager(tmp_path)
    await mgr.spawn(origin_session="s1", subagent_type="stub", description="d", prompt="p", **kw)
    await _drain_fire_tasks(mgr)


async def test_background_job_carries_the_turn_override(tmp_path, stub, fired):
    await _spawn(tmp_path, turn_model=OVERRIDE)
    assert fired()["model"] == OVERRIDE


async def test_background_job_runs_on_the_subagents_pinned_model(tmp_path, stub, fired):
    stub.register(PIN)
    await _spawn(tmp_path)  # no override: the pin alone still reaches the fire
    assert fired()["model"] == PIN
    await _spawn(tmp_path, turn_model=OVERRIDE)  # and it wins over an override
    assert fired()["model"] == PIN


async def test_background_job_without_override_or_pin_is_unchanged(tmp_path, stub, fired):
    await _spawn(tmp_path)
    assert "model" not in fired()  # the detached turn keeps the configured default


async def test_task_run_in_background_hands_the_override_to_the_fire(tmp_path, stub, fired):
    mgr = _manager(tmp_path)
    tools = {t.name: t for t in agent_mod._build_task_tools(LangGraphConfig(), [], background_mgr=mgr)}
    await tools["task"].ainvoke(
        {
            "name": "task",
            "args": {
                "description": "d",
                "prompt": "p",
                "subagent_type": "stub",
                "run_in_background": True,
                "state": {"model": OVERRIDE, "session_id": "s1"},
            },
            "id": "c-bg",
            "type": "tool_call",
        }
    )
    await _drain_fire_tasks(mgr)
    assert fired()["model"] == OVERRIDE
