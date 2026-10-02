"""A goal-driven turn ends as soon as the goal's verifier passes — end to end, through the
REAL streaming driver (``server.chat._chat_langgraph_stream``) and a real
``create_agent_graph`` (fake chat model). See ``tests/test_goal_checkpoint.py``.

The farm-b repro: ``/goal new`` + command verifier ``pytest -q``; the agent fixed the bug,
saw the tests pass, and kept going in the same turn — "the goal is already complete" two or
three more times — before a text-only reply let the post-turn verifier run. Now a passing
mid-turn probe records the goal achieved and runs ONE closing call with no tools bound, so
the reply ends on a short summary and no tool runs after the pass.
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


class _Clock:
    """A fake monotonic clock: each model round takes ``per_call`` seconds."""

    def __init__(self, per_call: float):
        self.t = 1000.0
        self.per_call = per_call

    def __call__(self):
        return self.t


class _ScriptedFake(GenericFakeChatModel):
    """Fake chat model with tool calls; ``side_effects[i]`` runs before call ``i`` answers.
    Records how many tools were bound for each call (``bound``)."""

    calls: int = 0
    side_effects: dict = {}
    clock: object = None
    bound: list = []
    _pending_bound: int = 0

    def bind_tools(self, tools, **kwargs):
        self._pending_bound = len(tools or [])
        return self

    async def _astream(self, messages, stop=None, run_manager=None, **kwargs):
        from langchain_core.messages import AIMessageChunk

        self.bound.append(self._pending_bound)
        self._pending_bound = 0
        if self.clock is not None:
            self.clock.t += self.clock.per_call
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


def _call(name: str, text: str, call_id: str, args: dict | None = None) -> AIMessage:
    return AIMessage(content=text, tool_calls=[{"name": name, "args": args or {}, "id": call_id, "type": "tool_call"}])


SUMMARY = "Fixed apply_discount to take a percentage; both tests pass."


def _install(monkeypatch, tmp_path, script, side_effects, *, per_call: float = 3.0):
    """A real lead graph on ``script`` + a real GoalController with a flag-file command goal.
    The DEFAULT probe debounce runs on a fake clock at ``per_call`` seconds per model round."""
    import runtime.state as rs
    from langgraph.checkpoint.memory import MemorySaver

    flag = tmp_path / "fixed.flag"
    clock = _Clock(per_call)
    monkeypatch.setattr("graph.middleware.goal_checkpoint._now", clock)
    fake = _ScriptedFake(
        messages=iter(script),
        side_effects={k: (lambda: flag.write_text("x")) for k in side_effects},
        clock=clock,
        bound=[],
    )
    cfg = LangGraphConfig(goal_max_iterations=8)
    # The goal-loop tools (update_goal_plan) bind only while a plugin verifier is
    # registered (#2690) — cowork registers one on a stock install; the registry is empty
    # in unit tests.
    monkeypatch.setattr("graph.goals.verifiers._PLUGIN_VERIFIERS", {"test:check": object()})
    with patch("graph.agent.create_llm", lambda *a, **k: fake):
        from graph.agent import create_agent_graph

        g = create_agent_graph(cfg, include_subagents=False, checkpointer=MemorySaver())
    ctrl = GoalController(cfg, GoalStore(tmp_path))
    monkeypatch.setattr(rs.STATE, "graph", g, raising=False)
    monkeypatch.setattr(rs.STATE, "graph_config", cfg, raising=False)
    monkeypatch.setattr(rs.STATE, "goal_controller", ctrl, raising=False)
    assert ctrl.set_goal_operator("gc1", "make the failing test pass", _flag_command(flag))[0]
    return fake, ctrl, g


async def _drive():
    frames = [f async for f in chat_mod._chat_langgraph_stream("Start working toward the goal", "gc1")]
    return frames, next(p for k, p in frames if k == "done")


async def _tools_after_the_pass(g) -> list[str]:
    """Tool results checkpointed AFTER the closing note — must be none."""
    from langchain_core.messages import ToolMessage

    from graph.middleware.guard_notes import is_guard_note

    snap = await g.aget_state({"configurable": {"thread_id": "a2a:gc1"}})
    msgs = snap.values["messages"]
    at = next(i for i, m in enumerate(msgs) if is_guard_note(m, "goal-checkpoint"))
    return [m.name for m in msgs[at:] if isinstance(m, ToolMessage)]


def _assert_closed(fake, ctrl, done, *, closing_call: int):
    assert fake.calls == closing_call + 1, "one closing call after the pass, then the turn ends"
    assert fake.bound[closing_call] == 0, "the closing call runs with no tools bound"
    assert all(n > 0 for n in fake.bound[:closing_call]), "working calls had their tools"
    assert "already complete" not in done and "explore" not in done
    # The reply ends on the closing summary, then the goal's terminal note.
    assert done.split("---")[0].rstrip().endswith(SUMMARY)
    assert done.rstrip().endswith("✓ goal achieved: command exited 0")
    state = ctrl.store.get("gc1")
    assert state.status == "achieved" and state.iteration == 0 and len(state.history) == 1


@pytest.mark.asyncio
async def test_plan_record_shape_closes_on_a_summary(monkeypatch, tmp_path):
    """plan → (fix) "Both tests pass" + plan → [pass] → closing summary."""
    script = [
        _call("update_goal_plan", "Exploring the repo first.", "p1", {"plan": "explore"}),
        _call("update_goal_plan", "Both tests pass.", "p2", {"plan": "done"}),
        AIMessage(content=SUMMARY),
        AIMessage(content="The goal is already complete."),
    ]
    fake, ctrl, g = _install(monkeypatch, tmp_path, script, side_effects={1})

    _frames, done = await _drive()

    _assert_closed(fake, ctrl, done, closing_call=2)
    assert await _tools_after_the_pass(g) == []


@pytest.mark.asyncio
async def test_review_repro_default_debounce_closes_right_after_the_fix(monkeypatch, tmp_path):
    """The round-2 review repro, with the DEFAULT debounce on a fake clock at 3s per model
    round: current_time (probe: not met) → calculator (the fix lands) → [probe: met] →
    closing call. A 10s floor suppressed the probe after the fix and the turn ran on to 5
    calls with "already complete" twice. The scripted closing answer even tries to call a
    tool — it has none bound, and the call is dropped."""
    script = [
        _call("current_time", "Looking around.", "t1"),
        _call("calculator", "Applying the fix.", "t2", {"expression": "1+1"}),
        _call("current_time", SUMMARY, "t3"),  # the closing call: its tool call must not run
        _call("current_time", "Both tests pass. Let me explore the project again…", "t4"),
        AIMessage(content="The goal is already complete — no further action needed."),
    ]
    fake, ctrl, g = _install(monkeypatch, tmp_path, script, side_effects={1})

    frames, done = await _drive()

    _assert_closed(fake, ctrl, done, closing_call=2)
    assert await _tools_after_the_pass(g) == []
    # The probe said so on the live stream — as a goal status line, never a tool_start.
    assert any(k == "goal_status" and "checking the goal" in str(p) for k, p in frames)
    assert not any(k == "tool_start" and "🎯" in str(p) for k, p in frames)
