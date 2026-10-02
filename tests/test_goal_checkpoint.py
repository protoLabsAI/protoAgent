"""A goal-driven turn ends as soon as the goal's verifier passes.

The live repro (farm-b, ``/goal new`` + command verifier ``pytest -q``): the agent fixed
the bug, saw both tests pass, recorded its plan — and then kept going in the SAME turn:
re-explored the repo, re-ran the tests, and said "the goal is already complete" two or
three more times before a text-only reply finally ended the turn and the post-turn
verifier marked it achieved (iteration 0, one history entry). The console showed it as
several runs stacked in one bubble.

``WaitYieldMiddleware`` (via ``graph.middleware.goal_checkpoint``) probes the verifier
right after the agent records its plan (``update_goal_plan``) on a goal-driven turn and
ends the turn when it passes.
"""

from __future__ import annotations

import importlib
import json
import sys
from unittest.mock import patch

import pytest
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain_core.outputs import ChatGenerationChunk

from graph.config import LangGraphConfig
from graph.goals.controller import GoalController
from graph.goals.goal_turn import goal_turn
from graph.goals.store import GoalStore
from graph.goals.types import VerifyResult
from graph.middleware.goal_checkpoint import just_recorded_plan
from graph.middleware.wait_yield import WaitYieldMiddleware

# The probe and the post-turn verifier spawn a real verifier command.
pytestmark = pytest.mark.platform_sensitive

chat_mod = importlib.import_module("server.chat")


def _plan_result(content: str = "plan recorded.", status: str | None = None) -> ToolMessage:
    kw = {"content": content, "tool_call_id": "c-plan", "name": "update_goal_plan"}
    if status:
        kw["status"] = status
    return ToolMessage(**kw)


def _flag_command(flag) -> dict:
    """A command verifier that passes once ``flag`` exists — portable (bash and cmd)."""
    return {
        "type": "command",
        "command": f'"{sys.executable}" -c "import os,sys; sys.exit(0 if os.path.exists(r\'{flag}\') else 1)"',
    }


# ── detection ─────────────────────────────────────────────────────────────────────────


def test_just_recorded_plan_reads_only_the_trailing_tool_block():
    assert just_recorded_plan([HumanMessage("go"), AIMessage("…"), _plan_result()])
    # ...alongside a parallel tool in the same round.
    other = ToolMessage(content="ok", tool_call_id="c-x", name="list_dir")
    assert just_recorded_plan([AIMessage("…"), _plan_result(), other])
    # A plan recorded in an EARLIER round doesn't count.
    assert not just_recorded_plan([_plan_result(), AIMessage("…"), other])
    # A fresh stimulus, a failed record, another tool.
    assert not just_recorded_plan([_plan_result(), AIMessage("done"), HumanMessage("next")])
    assert not just_recorded_plan([AIMessage("…"), _plan_result("no active goal for this session.")])
    assert not just_recorded_plan([AIMessage("…"), _plan_result("plan recorded.", status="error")])
    assert not just_recorded_plan([AIMessage("…"), other])


# ── the middleware ────────────────────────────────────────────────────────────────────


class _Ctrl:
    def __init__(self, met: bool | None):
        self.met = met
        self.probes: list[str] = []

    async def probe(self, session_id):
        self.probes.append(session_id)
        return None if self.met is None else VerifyResult(self.met, "command exited 0" if self.met else "exit 1", "")


@pytest.fixture
def ctrl(monkeypatch):
    import runtime.state as rs

    def _set(met):
        c = _Ctrl(met)
        monkeypatch.setattr(rs.STATE, "goal_controller", c, raising=False)
        return c

    return _set


_RECORDED = {"session_id": "s1", "messages": [AIMessage("Both tests pass."), _plan_result()]}


@pytest.mark.asyncio
async def test_ends_a_goal_turn_when_the_verifier_passes(ctrl):
    c = ctrl(True)
    with goal_turn() as marker:
        out = await WaitYieldMiddleware().abefore_model(_RECORDED, None)
    assert out == {"jump_to": "end"}
    assert c.probes == ["s1"] and marker.met_reason == "command exited 0"


@pytest.mark.asyncio
async def test_a_failing_probe_lets_the_agent_keep_working(ctrl):
    c = ctrl(False)
    with goal_turn() as marker:
        assert await WaitYieldMiddleware().abefore_model(_RECORDED, None) is None
    assert c.probes == ["s1"] and not marker.met_reason


@pytest.mark.asyncio
async def test_no_probe_off_a_goal_turn_or_without_a_plan_record(ctrl):
    c = ctrl(True)
    mw = WaitYieldMiddleware()
    assert await mw.abefore_model(_RECORDED, None) is None  # not a goal turn
    with goal_turn():
        other = {
            "session_id": "s1",
            "messages": [AIMessage("…"), ToolMessage(content="ok", tool_call_id="x", name="read_file")],
        }
        assert await mw.abefore_model(other, None) is None
    assert c.probes == []
    assert mw.before_model(_RECORDED, None) is None  # the sync hook never probes


# ── the controller probe ──────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_probe_runs_the_verifier_without_touching_goal_state(tmp_path):
    flag = tmp_path / "fixed.flag"
    ctrl = GoalController(LangGraphConfig(), GoalStore(tmp_path))
    assert ctrl.set_goal_operator("s1", "tests pass", _flag_command(flag))[0]

    assert (await ctrl.probe("s1")).met is False
    flag.write_text("x")
    assert (await ctrl.probe("s1")).met is True

    state = ctrl.active_goal("s1")
    assert state.status == "active" and state.iteration == 0 and state.history == []


@pytest.mark.asyncio
async def test_probe_skips_the_llm_judge_and_a_missing_goal(tmp_path):
    ctrl = GoalController(LangGraphConfig(), GoalStore(tmp_path))
    assert await ctrl.probe("nope") is None
    assert ctrl.set_goal_operator("s1", "write a nice poem", {"type": "llm"})[0]
    assert await ctrl.probe("s1") is None  # judges final text + costs a model call


def test_agent_wires_the_goal_checkpoint_without_a_new_graph_node():
    """Hosted in WaitYieldMiddleware: every before_model hook is one more node per round,
    i.e. one more step of every turn's recursion_limit."""
    from graph.agent import _build_middleware
    from graph.middleware import goal_checkpoint

    mws = _build_middleware(LangGraphConfig())
    assert any(isinstance(m, WaitYieldMiddleware) for m in mws)
    assert not any(type(m).__module__ == goal_checkpoint.__name__ for m in mws)


# ── end to end: the real streaming driver + a real create_agent graph ─────────────────


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


@pytest.mark.asyncio
async def test_goal_turn_stops_at_the_plan_record_that_meets_the_goal(monkeypatch, tmp_path):
    """The repro's shape: plan → (fix) → "Both tests pass" + plan. The turn ends there:
    the scripted "already complete" rounds are never requested, and the goal is achieved
    on iteration 0 with one history entry."""
    import runtime.state as rs
    from langgraph.checkpoint.memory import MemorySaver

    flag = tmp_path / "fixed.flag"
    script = [
        _plan_call("Exploring the repo first.", "p1"),  # probe: not met yet → keep working
        _plan_call("Both tests pass.", "p2"),  # probe: met → the turn ends here
        AIMessage(content="The goal is already complete.", tool_calls=[]),
        AIMessage(content="The goal is already complete — no further action needed."),
    ]
    # The "fix" lands while the model produces its second answer (as an edit_file in that
    # round would) — so the probe after p1 fails and the probe after p2 passes.
    fake = _ScriptedFake(messages=iter(script), side_effects={1: lambda: flag.write_text("x")})
    cfg = LangGraphConfig(goal_max_iterations=8)
    # The goal-loop tools bind only while a plugin verifier is registered (#2690); the
    # farm-b repro had one. The registry is empty in unit tests.
    monkeypatch.setattr("graph.goals.verifiers._PLUGIN_VERIFIERS", {"test:check": object()})
    with patch("graph.agent.create_llm", lambda *a, **k: fake):
        from graph.agent import create_agent_graph

        g = create_agent_graph(cfg, include_subagents=False, checkpointer=MemorySaver())
    ctrl = GoalController(cfg, GoalStore(tmp_path))
    monkeypatch.setattr(rs.STATE, "graph", g, raising=False)
    monkeypatch.setattr(rs.STATE, "graph_config", cfg, raising=False)
    monkeypatch.setattr(rs.STATE, "goal_controller", ctrl, raising=False)
    assert ctrl.set_goal_operator("gc1", "make the failing test pass", _flag_command(flag))[0]

    frames = [f async for f in chat_mod._chat_langgraph_stream("Start working toward the goal", "gc1")]

    assert fake.calls == 2, "the turn must end at the plan record whose probe passed"
    done = next(p for k, p in frames if k == "done")
    assert "already complete" not in done
    assert "goal achieved" in done
    state = ctrl.store.get("gc1")
    assert state.status == "achieved" and state.iteration == 0 and len(state.history) == 1
