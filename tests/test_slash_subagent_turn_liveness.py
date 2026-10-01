"""A ``/<subagent>`` slash turn under the A2A stall guard (#3977).

#3940 made a ``/<workflow>`` step report liveness each super-step. A ``/<subagent>``
slash run sent nothing between its ``tool_start`` and its ``tool_end``, so one run that
kept working longer than ``turn_stall_timeout_seconds`` (900s) was stopped as stalled.

* A slash subagent that keeps WORKING (model calls, tool rounds) outlives the window:
  each super-step becomes a rate-limited ``progress`` frame the guard counts — no card.
* A slash subagent wedged inside ONE call completes no super-step, sends nothing, and is
  still ended by the guard; its run is cancelled with the turn.
* Progress is scoped to the turn that started the run: a busy subagent in ANOTHER turn
  cannot keep a wedged one alive.
* A native turn's ``task()`` delegation already surfaces its subagent's tool rounds on
  the lead stream (callback propagation), so it outlives the window too — a guard, not a
  fix.

Drives the REAL chain: ``_pre_turn_dispatch`` → ``_run_parsed_subagent`` →
``run_manual_subagent`` → ``_run_subagent`` (real ``create_agent``, real middleware, a
real async tool) under the real ``_stall_guarded``; only the chat model is scripted.
"""

from __future__ import annotations

import asyncio

import pytest
from langchain_core.tools import tool

import graph.agent as agent_mod
import server.chat_commands as chat_commands
import server.chat_dispatch as chat_dispatch
from a2a_impl.executor import TurnStalled, _stall_guarded
from graph.config import LangGraphConfig
from graph.subagents.config import SUBAGENT_REGISTRY, SubagentConfig
from runtime.state import STATE
from tests.test_subagent_turn_budget import _ScriptedModel
from tests.test_workflow_turn_liveness import _calls

PROBE = "slash-liveness-probe"
# Wider than the workflow twin's (0.4s): this file also runs a whole lead graph, and
# every frame gap must stay well inside the window on a loaded CI box.
STALL_S = 1.5
TOOL_S = 0.3  # each tool round (each model call, natively): well inside the window
ROUNDS = 10  # ...but the whole run (3s+) is over two windows long


@pytest.fixture
def slash_state(monkeypatch):
    """Just enough STATE for the chain to reach the `/<subagent>` short-circuit."""
    monkeypatch.setattr(STATE, "goal_controller", None, raising=False)
    monkeypatch.setattr(STATE, "plugin_chat_commands", {}, raising=False)
    monkeypatch.setattr(STATE, "graph_config", LangGraphConfig(), raising=False)
    for attr in ("knowledge_store", "scheduler", "inbox_store", "tasks_store"):
        monkeypatch.setattr(STATE, attr, None, raising=False)
    monkeypatch.setattr(chat_commands, "_parse_slash_command", lambda m: ("", ""))
    monkeypatch.setattr(chat_commands, "_parse_workflow_command", lambda m: None)
    monkeypatch.setattr(chat_commands, "_parse_subagent_command", lambda m: (PROBE, m.split(" ", 1)[1]))
    monkeypatch.setattr(chat_dispatch, "_PROGRESS_MIN_INTERVAL_S", 0.0, raising=False)


@pytest.fixture
def busy_subagent(monkeypatch):
    """The probe subagent: ROUNDS tool rounds of an async tool taking TOOL_S each, run
    through the real `_run_parsed_subagent`. Returns the tool-execution log."""
    executed: list[str] = []

    @tool
    async def slow_ping() -> str:
        """Do a slow bit of work."""
        await asyncio.sleep(TOOL_S)
        executed.append("ping")
        return "pong"

    monkeypatch.setattr(agent_mod, "create_llm", lambda *_a, **_k: _ScriptedModel(rounds=ROUNDS))
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.setattr(agent_mod, "get_all_tools", lambda *_a, **_k: [slow_ping])
    monkeypatch.setitem(
        SUBAGENT_REGISTRY,
        PROBE,
        SubagentConfig(name=PROBE, description="d", system_prompt="p", tools=["slow_ping"], max_turns=ROUNDS + 2),
    )
    monkeypatch.setattr(_ScriptedModel, "_generate", _calls(slow_ping.name))
    return executed


async def _frames(stall_s: float = STALL_S, *, message: str = "/probe go", session: str = "s-slash"):
    pre = chat_dispatch._PreTurn(message)
    gen = _stall_guarded(chat_dispatch._pre_turn_dispatch(pre, session, None), stall_s, ["starting up"])
    frames = []
    async for frame in gen:
        frames.append(frame)
    return pre, frames


async def _warm_up(executed: list[str]) -> None:
    """Run once unguarded: the first model call in a process pays one-time costs
    (executor thread start, lazy imports) that can exceed a sub-second window."""
    await _frames(stall_s=0)
    executed.clear()


async def test_a_slash_subagent_that_keeps_working_outlives_the_stall_window(slash_state, busy_subagent):
    await _warm_up(busy_subagent)
    pre, frames = await _frames()

    assert len(busy_subagent) == ROUNDS  # every tool round ran — nothing was cut off
    assert pre.handled and frames[-1][0] == "done" and "FINAL ANSWER" in frames[-1][1]
    progress = [p for k, p in frames if k == "progress"]
    assert len(progress) >= ROUNDS  # a liveness frame per super-step
    assert all(p == {"id": f"subagent:{PROBE}", "subagent": PROBE} for p in progress)
    # Liveness frames are not cards: the visible frames are exactly the old ones.
    assert [(k, p["id"]) for k, p in frames[:-1] if k not in ("progress", "usage")] == [
        ("tool_start", f"subagent:{PROBE}"),
        ("tool_end", f"subagent:{PROBE}"),
    ]


async def test_slash_liveness_frames_are_rate_limited(slash_state, busy_subagent, monkeypatch):
    monkeypatch.setattr(chat_dispatch, "_PROGRESS_MIN_INTERVAL_S", 3600.0, raising=False)
    _pre, frames = await _frames(stall_s=0)  # guard off: count frames only

    assert len(busy_subagent) == ROUNDS
    assert sum(1 for k, _ in frames if k == "progress") == 1


def _wedged(monkeypatch, outcome: list[str]):
    """`_run_parsed_subagent` stuck inside one call that never returns."""

    async def run(subagent_type, prompt, *, session_id="", turn_model=""):
        try:
            await asyncio.sleep(3600)
        except asyncio.CancelledError:
            outcome.append("cancelled")
            raise
        return "unreachable"

    monkeypatch.setattr(chat_commands, "_run_parsed_subagent", run)


async def test_a_slash_subagent_wedged_in_one_call_is_still_ended_by_the_stall_guard(slash_state, monkeypatch):
    outcome: list[str] = []
    _wedged(monkeypatch, outcome)

    with pytest.raises(TurnStalled):
        await _frames(stall_s=0.2)
    assert outcome == ["cancelled"]  # the run ends with its turn, never detached


async def test_another_turns_busy_subagent_cannot_keep_a_wedged_one_alive(slash_state, busy_subagent, monkeypatch):
    """Two slash turns at once. Turn B's subagent keeps working (and reporting); turn A's
    is wedged. B's progress belongs to B: B survives the window, A is still stopped,
    and none of B's liveness reaches A."""
    await _warm_up(busy_subagent)
    real_run = chat_commands._run_parsed_subagent
    outcome: list[str] = []

    async def run(subagent_type, prompt, *, session_id="", turn_model=""):
        if prompt == "wedge":
            try:
                await asyncio.sleep(3600)
            except asyncio.CancelledError:
                outcome.append("A cancelled")
                raise
        return await real_run(subagent_type, prompt, session_id=session_id, turn_model=turn_model)

    monkeypatch.setattr(chat_commands, "_run_parsed_subagent", run)

    a_frames: list = []

    async def turn_a():
        pre = chat_dispatch._PreTurn("/probe wedge")
        gen = _stall_guarded(chat_dispatch._pre_turn_dispatch(pre, "s-a", None), STALL_S, ["starting up"])
        async for frame in gen:
            a_frames.append(frame)

    a, b = await asyncio.gather(turn_a(), _frames(message="/probe go", session="s-b"), return_exceptions=True)

    assert isinstance(a, TurnStalled), a
    assert outcome == ["A cancelled"]
    assert not [f for f in a_frames if f[0] == "progress"]
    assert not isinstance(b, BaseException), b
    b_pre, b_frames = b
    assert b_frames[-1][0] == "done" and "FINAL ANSWER" in b_frames[-1][1]
    assert len(busy_subagent) == ROUNDS


async def test_a_failing_slash_subagent_still_fails_the_turn(slash_state, monkeypatch):
    """Moving the run into its own task keeps its failure the turn's failure."""

    async def run(subagent_type, prompt, *, session_id="", turn_model=""):
        raise RuntimeError("boom")

    monkeypatch.setattr(chat_commands, "_run_parsed_subagent", run)
    with pytest.raises(RuntimeError, match="boom"):
        await _frames(stall_s=0)


# ── a native turn's task() / run_workflow delegation (verification, #3977) ───
#
# In-graph delegation needs no listener: LangChain propagates the lead run's callbacks
# into the sub-graph, so the subagent's tool rounds surface on the lead stream as tool
# cards, and each card is a frame the guard counts. These run a subagent over two
# windows long, in sub-window rounds, and pass on origin/main too — guards, not fixes.


def _slow_native_model(monkeypatch):
    """The nesting test's streaming fake, each model call taking TOOL_S."""
    from tests.test_subagent_nesting_stream import _ToolFake

    original = _ToolFake._astream

    async def _slow_astream(self, messages, stop=None, run_manager=None, **kwargs):
        await asyncio.sleep(TOOL_S)
        async for chunk in original(self, messages, stop=stop, run_manager=run_manager, **kwargs):
            yield chunk

    monkeypatch.setattr(_ToolFake, "_astream", _slow_astream)


def _rounds(tool_name: str):
    from langchain_core.messages import AIMessage

    return [
        AIMessage(content="", tool_calls=[{"name": tool_name, "args": {}, "id": f"s{i}", "type": "tool_call"}])
        for i in range(ROUNDS)
    ]


async def _guarded_native(install, session: str) -> list:
    """Warm up unguarded (the first graph run pays one-time costs), then one guarded turn
    on a freshly installed script."""
    from server.chat import _run_turn_stream

    install()
    async for _frame in _run_turn_stream(
        "warm up", f"{session}-warm", {"configurable": {"thread_id": f"{session}-warm"}}
    ):
        pass
    install()
    frames = []
    gen = _stall_guarded(
        _run_turn_stream("go", session, {"configurable": {"thread_id": session}}), STALL_S, ["starting up"]
    )
    async for frame in gen:
        frames.append(frame)
    return frames


async def test_a_native_task_delegation_that_keeps_working_outlives_the_stall_window(monkeypatch):
    from langchain_core.messages import AIMessage

    from tests.test_subagent_nesting_stream import _delegate, _install

    _slow_native_model(monkeypatch)
    script = [_delegate(description="check", prompt="what time is it", subagent_type="researcher")]
    script += _rounds("current_time")
    script += [AIMessage(content="it is noon"), AIMessage(content="the subagent says it is noon")]

    frames = await _guarded_native(lambda: _install(monkeypatch, script), "s-native-task")

    nested_ends = [p for k, p in frames if k == "tool_end" and p.get("parentId") == "t1"]
    assert len(nested_ends) == ROUNDS  # every subagent round ran and surfaced
    assert any(k == "tool_end" and p.get("name") == "task" for k, p in frames)


async def test_a_native_run_workflow_step_that_keeps_working_outlives_the_stall_window(monkeypatch):
    """The workflows plugin's tool shape: a tool body runs its step through
    ``sdk.run_subagent`` (no ``parent_task_id``) — its rounds still surface."""
    import itertools

    import runtime.state as rs
    from langchain_core.messages import AIMessage
    from langgraph.checkpoint.memory import MemorySaver

    from graph import sdk
    from tests.test_subagent_nesting_stream import _ToolFake

    @tool
    async def slow_ping() -> str:
        """Do a slow bit of work."""
        await asyncio.sleep(TOOL_S)
        return "pong"

    @tool
    async def run_workflow(name: str) -> str:
        """Run a one-step workflow."""
        return await sdk.run_subagent(PROBE, "step", description=f"{name}:gather", extra_tools=[slow_ping])

    monkeypatch.setitem(
        SUBAGENT_REGISTRY,
        PROBE,
        SubagentConfig(name=PROBE, description="d", system_prompt="p", tools=["slow_ping"], max_turns=ROUNDS + 2),
    )
    monkeypatch.setattr(agent_mod, "get_all_tools", lambda *_a, **_k: [])
    for attr in ("knowledge_store", "scheduler", "inbox_store", "tasks_store", "plugin_tools", "mcp_tools"):
        monkeypatch.setattr(rs.STATE, attr, None, raising=False)
    monkeypatch.setattr(rs.STATE, "goal_controller", None, raising=False)
    monkeypatch.setattr(rs.STATE, "graph_config", LangGraphConfig(), raising=False)
    _slow_native_model(monkeypatch)
    lead_call = AIMessage(
        content="", tool_calls=[{"name": "run_workflow", "args": {"name": "w"}, "id": "w1", "type": "tool_call"}]
    )
    script = [lead_call, *_rounds("slow_ping"), AIMessage(content="step done"), AIMessage(content="workflow done")]

    def install():
        fake = _ToolFake(messages=itertools.chain(iter(script), itertools.repeat(AIMessage(content="done"))))
        monkeypatch.setattr(agent_mod, "create_llm", lambda *a, **k: fake)
        graph = agent_mod.create_agent_graph(
            LangGraphConfig(), include_subagents=False, checkpointer=MemorySaver(), extra_tools=[run_workflow]
        )
        monkeypatch.setattr(rs.STATE, "graph", graph, raising=False)

    frames = await _guarded_native(install, "s-native-wf")

    assert sum(1 for k, p in frames if k == "tool_end" and p.get("name") == "slow_ping") == ROUNDS
    assert any(k == "tool_end" and p.get("name") == "run_workflow" for k, p in frames)
