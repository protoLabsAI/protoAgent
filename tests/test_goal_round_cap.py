"""The per-turn round cap for goal-driven turns (#3957).

An unsatisfiable goal ("write x each turn" + a verifier that can never pass) once spun a
single turn for 130+ model calls — ``model.round_hard_cap`` is off by default and nothing
else bounded a goal turn's rounds. ``goal.max_rounds_per_turn`` (default 50) caps a
GOAL-DRIVEN turn only; the effective cap there is the smaller non-zero of it and
``model.round_hard_cap``; 0 on both is unlimited. A capped goal turn ends with a hand-back,
and the goal drive PAUSES (goal stays active, reason on its timeline) instead of
immediately re-driving another runaway turn.
"""

from __future__ import annotations

import contextvars
import importlib

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

import server.goal_loop as goal_loop
from graph.config import LangGraphConfig
from graph.goals.goal_turn import GoalTurn, goal_turn, in_goal_turn, record_round_cap
from graph.middleware.round_governor import RoundGovernorMiddleware
from tests._turn_driver_fakes import FakeGoals, ScriptedGraph, text, turn_result


def _turn(rounds: int):
    msgs = [HumanMessage(content="write x")]
    for i in range(rounds):
        msgs.append(
            AIMessage(content="", tool_calls=[{"name": "write_file", "args": {}, "id": f"c{i}", "type": "tool_call"}])
        )
        msgs.append(ToolMessage(content="ok", tool_call_id=f"c{i}"))
    return msgs


def _ends_at(mw: RoundGovernorMiddleware, *, goal: bool, upto: int = 300) -> int | None:
    """The round count at which ``mw`` first ends the turn (``None`` = never, up to ``upto``)."""
    with goal_turn(goal):
        for n in range(upto + 1):
            out = mw.before_model({"messages": _turn(n)}, None)
            if out and out.get("jump_to") == "end":
                return n
    return None


# ── the governor ──────────────────────────────────────────────────────────────────────


def test_goal_turn_stops_at_the_goal_cap_with_a_graceful_close():
    mw = RoundGovernorMiddleware(nudge_after=0, hard_cap=0, goal_cap=8)
    with goal_turn() as marker:
        assert mw.before_model({"messages": _turn(7)}, None) is None
        out = mw.before_model({"messages": _turn(8)}, None)
    assert out["jump_to"] == "end"
    close = out["messages"][0]
    assert isinstance(close, AIMessage)
    assert "goal.max_rounds_per_turn: 8" in close.content and "goal stays active" in close.content
    # ...and the pass is marked capped, for the goal drive.
    assert marker.capped and (marker.rounds, marker.round_cap, marker.cap_key) == (8, 8, "goal.max_rounds_per_turn")


def test_a_non_goal_turn_is_unaffected_by_the_goal_cap():
    mw = RoundGovernorMiddleware(nudge_after=0, hard_cap=0, goal_cap=8)
    assert _ends_at(mw, goal=False) is None


def test_goal_cap_zero_is_unlimited_when_the_global_cap_is_off():
    assert _ends_at(RoundGovernorMiddleware(nudge_after=0, hard_cap=0, goal_cap=0), goal=True) is None


def test_goal_cap_zero_falls_back_to_the_global_round_hard_cap():
    mw = RoundGovernorMiddleware(nudge_after=0, hard_cap=10, goal_cap=0)
    assert _ends_at(mw, goal=True) == 10
    with goal_turn() as marker:
        out = mw.before_model({"messages": _turn(10)}, None)
    assert "model.round_hard_cap: 10" in out["messages"][0].content
    assert marker.cap_key == "model.round_hard_cap"  # still pauses the drive


@pytest.mark.parametrize(
    ("goal_cap", "hard_cap", "goal_ends", "plain_ends"), [(8, 20, 8, 20), (30, 12, 12, 12), (8, 0, 8, None)]
)
def test_effective_goal_cap_is_the_smaller_non_zero_cap(goal_cap, hard_cap, goal_ends, plain_ends):
    mw = RoundGovernorMiddleware(nudge_after=0, hard_cap=hard_cap, goal_cap=goal_cap)
    assert _ends_at(mw, goal=True) == goal_ends
    assert _ends_at(mw, goal=False) == plain_ends


def test_non_goal_hard_cap_message_unchanged():
    out = RoundGovernorMiddleware(nudge_after=0, hard_cap=8, goal_cap=50).before_model({"messages": _turn(8)}, None)
    assert "this turn has run 8 model rounds" in out["messages"][0].content
    assert "model.round_hard_cap: 8" in out["messages"][0].content


def test_round_cap_record_crosses_a_copied_context():
    """LangGraph runs a node in a COPY of the invoking context: the record must still land
    on the driver's marker."""
    with goal_turn() as marker:
        contextvars.copy_context().run(record_round_cap, 8, 8, "goal.max_rounds_per_turn")
    assert marker.capped and marker.rounds == 8
    record_round_cap(9, 9, "x")  # outside a goal turn: a no-op
    assert not in_goal_turn()


# ── config ────────────────────────────────────────────────────────────────────────────


def test_config_default_and_yaml_key():
    assert LangGraphConfig().goal_max_rounds_per_turn == 50
    assert LangGraphConfig.from_dict({"goal": {"max_rounds_per_turn": 12}}).goal_max_rounds_per_turn == 12
    assert LangGraphConfig.from_dict({"goal": {"max_rounds_per_turn": 0}}).goal_max_rounds_per_turn == 0


def test_settings_field_accepts_zero_for_unlimited():
    from graph.settings_schema import FIELDS

    f = next(f for f in FIELDS if f.key == "goal.max_rounds_per_turn")
    assert f.attr == "goal_max_rounds_per_turn" and f.section == "Goal mode" and f.type == "number"
    assert f.minimum == 0 and "0 = unlimited" in f.description and not f.restart


def test_agent_wires_the_goal_cap_into_the_governor():
    from graph.agent import _build_middleware

    cfg = LangGraphConfig.from_dict({"goal": {"max_rounds_per_turn": 7}, "model": {"round_hard_cap": 30}})
    gov = next(m for m in _build_middleware(cfg) if isinstance(m, RoundGovernorMiddleware))
    with goal_turn():
        assert gov.effective_cap() == (7, "goal.max_rounds_per_turn")
    assert gov.effective_cap() == (30, "model.round_hard_cap")


# ── the goal drive: a capped pass pauses it, bounded ─────────────────────────────────


@pytest.fixture
def state(monkeypatch):
    import runtime.state as rs

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
    }.items():
        monkeypatch.setattr(rs.STATE, attr, val, raising=False)
    return rs.STATE


class _NeverMet(FakeGoals):
    """A verifier that can never pass — every evaluate says continue — and records the
    round-cap pause the drive reports."""

    def __init__(self):
        super().__init__(forever=("continue", "not met", "keep going"), iteration=1)
        self.round_caps: list[str] = []

    def note_round_cap(self, session_id, reason):
        self.round_caps.append(reason)


def _capping_hook(cap_on_calls: set[int]):
    """A graph ``on_call`` hook that runs the REAL governor at its cap on the listed calls
    (1-based) — as a runaway pass would hit it."""
    calls = {"n": 0}
    mw = RoundGovernorMiddleware(nudge_after=0, hard_cap=0, goal_cap=8)

    def hook(graph, config):
        calls["n"] += 1
        if calls["n"] in cap_on_calls:
            assert mw.before_model({"messages": _turn(8)}, None)["jump_to"] == "end"

    return hook


async def _run(state, monkeypatch, surface, n_passes, hook):
    ctrl = _NeverMet()
    monkeypatch.setattr(state, "goal_controller", ctrl, raising=False)
    if surface == "stream":
        g = ScriptedGraph(streams=[[text(f"r{i}", f"pass{i}")] for i in range(n_passes)])
    else:
        g = ScriptedGraph(invokes=[turn_result(AIMessage(content=f"pass{i}")) for i in range(n_passes)])
    g.on_call = hook
    monkeypatch.setattr(state, "graph", g, raising=False)
    # By path: ``server`` re-exports the ``chat`` FUNCTION under the submodule's name.
    chat = importlib.import_module("server.chat")
    if surface == "stream":
        frames = [f async for f in chat._chat_langgraph_stream("go", "s1", request_metadata={})]
        final, calls = frames[-1][1], len(g.stream_calls)
    else:
        final, calls = (await chat.chat("go", "s1"))[0]["content"], len(g.invoke_calls)
    return ctrl, final, calls


@pytest.mark.asyncio
@pytest.mark.parametrize("surface", ["stream", "sync"])
async def test_a_capped_initial_goal_turn_pauses_the_drive(state, monkeypatch, surface):
    ctrl, final, calls = await _run(state, monkeypatch, surface, 3, _capping_hook({1}))
    assert calls == 1  # no continuation re-drives the runaway turn
    assert len(ctrl.evals) == 1  # ...but the verifier still judged it (a met goal would finish)
    assert final.startswith("pass0") and "round cap reached" in final and "goal.max_rounds_per_turn: 8" in final
    assert len(ctrl.round_caps) == 1 and "round cap reached" in ctrl.round_caps[0]


@pytest.mark.asyncio
@pytest.mark.parametrize("surface", ["stream", "sync"])
async def test_a_capped_continuation_pauses_the_drive(state, monkeypatch, surface):
    ctrl, final, calls = await _run(state, monkeypatch, surface, 4, _capping_hook({2}))
    assert calls == 2 and len(ctrl.evals) == 2
    assert final.startswith("pass1") and "round cap reached" in final
    assert len(ctrl.round_caps) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("surface", ["stream", "sync"])
async def test_uncapped_goal_turns_still_run_to_the_drive_bound(state, monkeypatch, surface):
    """No cap hit → the drive is unchanged: continuations up to its hard bound."""
    bound = state.graph_config.goal_max_iterations + 2
    ctrl, final, calls = await _run(state, monkeypatch, surface, bound + 1, lambda g, c: None)
    assert calls == bound + 1 and ctrl.round_caps == []
    assert "round cap reached" not in final


def test_goal_drive_pauses_only_on_a_capped_pass():
    assert goal_loop.GoalContinuation("m", {}).goal_pass is None
    capped = GoalTurn(round_cap=8, rounds=8, cap_key="goal.max_rounds_per_turn")
    note = goal_loop.round_cap_note(capped)
    assert "round cap reached (8 model rounds in one turn; goal.max_rounds_per_turn: 8)" in note


def test_controller_records_the_round_cap_and_keeps_the_goal_active(tmp_path):
    from graph.goals.controller import GoalController
    from graph.goals.store import GoalStore
    from graph.goals.types import GoalState

    ctrl = GoalController(LangGraphConfig(), GoalStore(tmp_path))
    ctrl.store.set(GoalState(session_id="s1", condition="write x", verifier={"type": "llm"}))
    ctrl.note_round_cap("s1", "⏸ goal paused — round cap reached")
    st = ctrl.active_goal("s1")
    assert st is not None and st.history[-1]["status"] == "round_cap"
    assert "round cap reached" in st.history[-1]["reason"]
    ctrl.note_round_cap("nope", "x")  # no goal: a no-op
