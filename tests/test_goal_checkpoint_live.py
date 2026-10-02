"""A goal-driven turn ends as soon as the goal's verifier passes — end to end, through the
REAL streaming driver (``server.chat._chat_langgraph_stream``) and a real
``create_agent_graph`` (fake chat model). See ``tests/test_goal_checkpoint.py``.

The farm-b repro: ``/goal new`` + command verifier ``pytest -q``; the agent fixed the bug,
saw the tests pass, and kept going in the same turn — "the goal is already complete" two or
three more times — before a text-only reply let the post-turn verifier run.
"""

from __future__ import annotations

import importlib
import json
import sys
from unittest.mock import patch

import pytest
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage
from langchain_core.outputs import ChatGenerationChunk

from graph.config import LangGraphConfig
from graph.goals.controller import GoalController
from graph.goals.store import GoalStore

# The probe and the post-turn verifier spawn a real verifier command.
pytestmark = pytest.mark.platform_sensitive

chat_mod = importlib.import_module("server.chat")


def _flag_command(flag) -> dict:
    """A command verifier that passes once ``flag`` exists — portable (bash and cmd)."""
    return {
        "type": "command",
        "command": f'"{sys.executable}" -c "import os,sys; sys.exit(0 if os.path.exists(r\'{flag}\') else 1)"',
    }


class _ScriptedFake(GenericFakeChatModel):
    """Fake chat model with tool calls; ``side_effects[i]`` runs before call ``i`` answers."""

    calls: int = 0
    side_effects: dict = {}

    def bind_tools(self, tools, **kwargs):
        return self

    async def _astream(self, messages, stop=None, run_manager=None, **kwargs):
        from langchain_core.messages import AIMessageChunk

        effect = self.side_effects.get(self.calls)
        self.calls += 1
        if effect:
            effect()
        message = next(self.messages)
        chunks = [
            {"name": tc["name"], "args": json.dumps(tc["args"]), "id": tc["id"], "index": i, "type": "tool_call_chunk"}
            for i, tc in enumerate(getattr(message, "tool_calls", []) or [])
        ]
        yield ChatGenerationChunk(message=AIMessageChunk(content=message.content or "", tool_call_chunks=chunks))


def _plan_call(text: str, call_id: str) -> AIMessage:
    return AIMessage(
        content=text,
        tool_calls=[{"name": "update_goal_plan", "args": {"plan": text or "plan"}, "id": call_id, "type": "tool_call"}],
    )


def _call(name: str, text: str, call_id: str, args: dict | None = None) -> AIMessage:
    return AIMessage(content=text, tool_calls=[{"name": name, "args": args or {}, "id": call_id, "type": "tool_call"}])


def _install(monkeypatch, tmp_path, script, side_effects):
    """A real lead graph on ``script`` + a real GoalController with a flag-file command goal."""
    import runtime.state as rs
    from langgraph.checkpoint.memory import MemorySaver

    flag = tmp_path / "fixed.flag"
    fake = _ScriptedFake(messages=iter(script), side_effects={k: (lambda: flag.write_text("x")) for k in side_effects})
    cfg = LangGraphConfig(goal_max_iterations=8)
    # The goal-loop tools (update_goal_plan) bind only while a plugin verifier is
    # registered (#2690) — cowork registers one on a stock install; the registry is empty
    # in unit tests.
    monkeypatch.setattr("graph.goals.verifiers._PLUGIN_VERIFIERS", {"test:check": object()})
    # No debounce between probes here (test_goal_checkpoint.py covers it): every tool round
    # may probe.
    monkeypatch.setattr("graph.middleware.goal_checkpoint.PROBE_MIN_INTERVAL_S", 0.0)
    monkeypatch.setattr("graph.middleware.goal_checkpoint.PROBE_BACKOFF_FACTOR", 0.0)
    with patch("graph.agent.create_llm", lambda *a, **k: fake):
        from graph.agent import create_agent_graph

        g = create_agent_graph(cfg, include_subagents=False, checkpointer=MemorySaver())
    ctrl = GoalController(cfg, GoalStore(tmp_path))
    monkeypatch.setattr(rs.STATE, "graph", g, raising=False)
    monkeypatch.setattr(rs.STATE, "graph_config", cfg, raising=False)
    monkeypatch.setattr(rs.STATE, "goal_controller", ctrl, raising=False)
    assert ctrl.set_goal_operator("gc1", "make the failing test pass", _flag_command(flag))[0]
    return fake, ctrl


async def _drive():
    frames = [f async for f in chat_mod._chat_langgraph_stream("Start working toward the goal", "gc1")]
    return frames, next(p for k, p in frames if k == "done")


@pytest.mark.asyncio
async def test_goal_turn_stops_at_the_plan_record_that_meets_the_goal(monkeypatch, tmp_path):
    """The repro's shape: plan → (fix) → "Both tests pass" + plan. The turn ends there:
    the scripted "already complete" rounds are never requested, and the goal is achieved
    on iteration 0 with one history entry."""
    script = [
        _call("update_goal_plan", "Exploring the repo first.", "p1", {"plan": "explore"}),  # not met yet
        _call("update_goal_plan", "Both tests pass.", "p2", {"plan": "done"}),  # met → ends here
        AIMessage(content="The goal is already complete."),
        AIMessage(content="The goal is already complete — no further action needed."),
    ]
    # The "fix" lands while the model produces its second answer (as an edit_file in that
    # round would) — so the probe after p1 fails and the probe after p2 passes.
    fake, ctrl = _install(monkeypatch, tmp_path, script, side_effects={1})

    _frames, done = await _drive()

    assert fake.calls == 2, "the turn must end at the tool round whose probe passed"
    assert "already complete" not in done
    assert "goal achieved" in done
    state = ctrl.store.get("gc1")
    assert state.status == "achieved" and state.iteration == 0 and len(state.history) == 1


@pytest.mark.asyncio
async def test_goal_turn_stops_after_any_tool_round_not_only_a_plan_record(monkeypatch, tmp_path):
    """The review's alternate tool order: no ``update_goal_plan`` at all. calculator (the fix
    lands) → current_time "Both tests pass. Let me explore…" → current_time "already
    complete" → text. The first probe after the fix ends the turn."""
    script = [
        _call("calculator", "Applying the fix.", "t1", {"expression": "1+1"}),
        _call("current_time", "Both tests pass. Let me explore the project again…", "t2"),
        _call("current_time", "The goal is already complete.", "t3"),
        AIMessage(content="The goal is already complete — no further action needed."),
    ]
    fake, ctrl = _install(monkeypatch, tmp_path, script, side_effects={0})

    frames, done = await _drive()

    assert fake.calls == 1, "the turn must end at the first tool round after the fix"
    assert "already complete" not in done and "explore" not in done
    assert "goal achieved" in done
    state = ctrl.store.get("gc1")
    assert state.status == "achieved" and state.iteration == 0 and len(state.history) == 1
    # The probe said so on the live stream while it ran.
    assert any(k == "tool_start" and "checking the goal" in str(p) for k, p in frames)
