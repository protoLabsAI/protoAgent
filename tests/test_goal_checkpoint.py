"""A goal-driven turn ends as soon as the goal's verifier passes (unit level; the real
streaming driver + graph run in ``tests/test_goal_checkpoint_live.py``).

The live repro (farm-b, ``/goal new`` + command verifier ``pytest -q``): the agent fixed
the bug, saw both tests pass — and kept going in the SAME turn, saying "the goal is
already complete" two or three more times before a text-only reply let the post-turn
verifier mark it achieved. ``WaitYieldMiddleware`` (via ``graph.middleware.goal_checkpoint``)
probes the verifier after each tool round of a goal-driven turn, debounced, and ends the
turn when it passes.
"""

from __future__ import annotations

import sys

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from graph.config import LangGraphConfig
from graph.goals.controller import GoalController
from graph.goals.goal_turn import goal_turn
from graph.goals.store import GoalStore
from graph.goals.types import VerifyResult
from graph.middleware import goal_checkpoint
from graph.middleware.goal_checkpoint import after_tool_round
from graph.middleware.wait_yield import WaitYieldMiddleware

# The controller-probe tests spawn a real verifier command.
pytestmark = pytest.mark.platform_sensitive


def _tool(name: str = "edit_file", content: str = "ok", n: int = 0) -> ToolMessage:
    return ToolMessage(content=content, tool_call_id=f"c-{name}-{n}", name=name)


def _flag_command(flag) -> dict:
    """A command verifier that passes once ``flag`` exists — portable (bash and cmd)."""
    return {
        "type": "command",
        "command": f'"{sys.executable}" -c "import os,sys; sys.exit(0 if os.path.exists(r\'{flag}\') else 1)"',
    }


def _rounds(n: int) -> list:
    """A turn ``n`` tool rounds in: Human, then (AI → Tool) × n."""
    msgs: list = [HumanMessage("go")]
    for i in range(n):
        msgs += [AIMessage("…"), _tool(n=i)]
    return msgs


# ── detection ─────────────────────────────────────────────────────────────────────────


def test_after_tool_round_is_any_tool_result_at_the_tail():
    assert after_tool_round([HumanMessage("go"), AIMessage("…"), _tool("calculator")])
    assert after_tool_round([AIMessage("…"), _tool("update_goal_plan", "plan recorded.")])
    assert not after_tool_round([HumanMessage("go")])  # a fresh stimulus
    assert not after_tool_round([_tool(), AIMessage("done")])
    assert not after_tool_round([])


# ── the middleware ────────────────────────────────────────────────────────────────────


class _Ctrl:
    def __init__(self, met: bool | None, *, eligible: bool = True, clock=None, takes: float = 0.0):
        self.met = met
        self.eligible = eligible
        self.probes: list[str] = []
        self._clock = clock
        self._takes = takes

    def can_probe(self, session_id):
        return self.eligible

    async def probe(self, session_id):
        self.probes.append(session_id)
        if self._clock is not None:
            self._clock.t += self._takes
        return None if self.met is None else VerifyResult(self.met, "command exited 0" if self.met else "exit 1", "")


class _Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


@pytest.fixture
def clock(monkeypatch):
    c = _Clock()
    monkeypatch.setattr(goal_checkpoint, "_now", c)
    return c


@pytest.fixture
def ctrl(monkeypatch, clock):
    import runtime.state as rs

    def _set(met, **kw):
        c = _Ctrl(met, clock=clock, **kw)
        monkeypatch.setattr(rs.STATE, "goal_controller", c, raising=False)
        return c

    return _set


def _state(n: int = 1) -> dict:
    return {"session_id": "s1", "messages": _rounds(n)}


@pytest.mark.asyncio
async def test_ends_a_goal_turn_when_the_verifier_passes(ctrl):
    c = ctrl(True)
    with goal_turn() as marker:
        out = await WaitYieldMiddleware().abefore_model(_state(), None)
    assert out == {"jump_to": "end"}
    assert c.probes == ["s1"] and marker.probes == 1


@pytest.mark.asyncio
async def test_a_failing_probe_lets_the_agent_keep_working(ctrl):
    c = ctrl(False)
    with goal_turn():
        assert await WaitYieldMiddleware().abefore_model(_state(), None) is None
    assert c.probes == ["s1"]


@pytest.mark.asyncio
async def test_no_probe_off_a_goal_turn_before_a_tool_round_or_for_an_ineligible_goal(ctrl):
    c = ctrl(True)
    mw = WaitYieldMiddleware()
    assert await mw.abefore_model(_state(), None) is None  # not a goal turn
    with goal_turn():
        assert await mw.abefore_model({"session_id": "s1", "messages": [HumanMessage("go")]}, None) is None
    assert c.probes == []
    assert mw.before_model(_state(), None) is None  # the sync hook never probes
    c2 = ctrl(True, eligible=False)  # e.g. an llm verifier
    with goal_turn():
        assert await mw.abefore_model(_state(), None) is None
    assert c2.probes == []


@pytest.mark.asyncio
async def test_at_most_one_probe_per_round(ctrl, clock):
    c = ctrl(False)
    mw = WaitYieldMiddleware()
    with goal_turn():
        await mw.abefore_model(_state(1), None)
        clock.t += 3600  # even long after: the SAME round is never probed twice
        await mw.abefore_model(_state(1), None)
    assert len(c.probes) == 1


@pytest.mark.asyncio
async def test_probes_are_bounded_over_many_quick_rounds(ctrl, clock):
    """30 tool rounds 1s apart with a 1s verifier: one probe, then one per ≥10s window —
    not one per round (the repro ran the command verifier 5x in one turn)."""
    c = ctrl(False, takes=1.0)
    mw = WaitYieldMiddleware()
    with goal_turn() as marker:
        for n in range(1, 31):
            await mw.abefore_model(_state(n), None)
            clock.t += 1.0
    # ~41s of turn at ≥ 10s + the 1s probe → at most 4 probes, vs 30 rounds.
    assert 1 < len(c.probes) <= 4 and marker.probes == len(c.probes)


@pytest.mark.asyncio
async def test_a_slow_verifier_backs_off_to_twice_its_duration(ctrl, clock):
    c = ctrl(False, takes=60.0)
    mw = WaitYieldMiddleware()
    with goal_turn() as marker:
        await mw.abefore_model(_state(1), None)
        assert marker.probe_after == pytest.approx(clock.t + 120.0)
        clock.t += 100.0
        await mw.abefore_model(_state(2), None)  # 100s < 120s → debounced
        clock.t += 25.0
        await mw.abefore_model(_state(3), None)
    assert len(c.probes) == 2


@pytest.mark.asyncio
async def test_the_debounce_is_per_pass(ctrl, clock):
    c = ctrl(False)
    mw = WaitYieldMiddleware()
    with goal_turn():
        await mw.abefore_model(_state(1), None)
    with goal_turn():  # a goal continuation is a new pass
        await mw.abefore_model(_state(1), None)
    assert len(c.probes) == 2


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
