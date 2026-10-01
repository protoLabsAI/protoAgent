"""A ``/<workflow>`` turn under the A2A stall guard (#3940, #3957).

* A step that keeps WORKING (its subagent completing model calls and tool rounds) is
  never cut off for being quiet: each super-step becomes a liveness frame the stall guard
  counts. Before, a workflow emitted frames only at step start/end, so one step longer
  than ``turn_stall_timeout_seconds`` cancelled the whole run (a #3938 behaviour change).
* A step wedged inside ONE call completes no super-step, sends nothing, and is still
  ended by the guard — the bound on a genuinely hung step.
* The settle wait on an abandoned runner can itself be cancelled (the guard gives the
  whole stream close 5s; the wait allows 10s) — the runner's late outcome is still
  retrieved, never "Task exception was never retrieved".
* A run with a failed step ends the turn FAILED, not COMPLETED with "Error: …" as the
  answer (#3957).

The liveness tests drive the REAL subagent runner (``graph.agent._run_subagent``: real
``create_agent``, real middleware stack, a real async tool) with only the chat model
scripted, under the real ``_stall_guarded`` and the real dispatch chain.
"""

from __future__ import annotations

import asyncio
import gc
import types

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

PROBE = "liveness-probe"
STALL_S = 0.4
TOOL_S = 0.15  # each tool round: well inside the window
ROUNDS = 8  # ...but the whole step (~1.2s+) is three windows long


@pytest.fixture
def quiet_state(monkeypatch):
    """Just enough STATE for the chain to reach the workflow short-circuit."""
    monkeypatch.setattr(STATE, "goal_controller", None, raising=False)
    monkeypatch.setattr(STATE, "plugin_chat_commands", {}, raising=False)
    monkeypatch.setattr(
        STATE,
        "graph_config",
        types.SimpleNamespace(agent_runtime="native", max_iterations=5, operator_mcp_tools=[], acp_agents={}),
        raising=False,
    )
    monkeypatch.setattr(chat_commands, "_parse_slash_command", lambda m: ("", ""))
    monkeypatch.setattr(chat_commands, "_parse_workflow_command", lambda m: ("brief", {"topic": "x"}))
    monkeypatch.setattr(chat_dispatch, "_WORKFLOW_PROGRESS_MIN_INTERVAL_S", 0.0, raising=False)


@pytest.fixture
def busy_step(monkeypatch):
    """A workflow whose one step runs the real subagent runner: ROUNDS tool rounds of an
    async tool that takes TOOL_S each. Returns the tool-execution log."""
    executed: list[str] = []

    @tool
    async def slow_ping() -> str:
        """Do a slow bit of work."""
        await asyncio.sleep(TOOL_S)
        executed.append("ping")
        return "pong"

    monkeypatch.setattr(agent_mod, "create_llm", lambda *_a, **_k: _ScriptedModel(rounds=ROUNDS))
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.setitem(
        SUBAGENT_REGISTRY,
        PROBE,
        SubagentConfig(name=PROBE, description="d", system_prompt="p", tools=["slow_ping"], max_turns=ROUNDS + 2),
    )
    # The `slow_ping` tool's own name is what the scripted model calls.
    monkeypatch.setattr(_ScriptedModel, "_generate", _calls(slow_ping.name))

    async def run(name, inputs, *, on_step=None):
        await on_step({"phase": "start", "step_id": "gather", "subagent": PROBE})
        out = await agent_mod._run_subagent(
            config=LangGraphConfig(),
            tool_map={slow_ping.name: slow_ping},
            available_subagents=PROBE,
            description="workflow brief:gather",
            prompt="go",
            subagent_type=PROBE,
        )
        await on_step({"phase": "end", "step_id": "gather", "output": out})
        return out

    monkeypatch.setattr(chat_commands, "_run_parsed_workflow", run)
    return executed


def _calls(tool_name: str):
    from langchain_core.messages import AIMessage
    from langchain_core.outputs import ChatGeneration, ChatResult

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        i = self.calls
        self.calls += 1
        if i < self.rounds:
            msg = AIMessage(content="", tool_calls=[{"name": tool_name, "args": {}, "id": f"call-{i}"}])
        else:
            msg = AIMessage(content="FINAL ANSWER")
        return ChatResult(generations=[ChatGeneration(message=msg)])

    return _generate


async def _guarded_frames(stall_s: float = STALL_S):
    pre = chat_dispatch._PreTurn("/brief x")
    gen = _stall_guarded(chat_dispatch._pre_turn_dispatch(pre, "s-live", None), stall_s, ["starting up"])
    frames = []
    async for frame in gen:
        frames.append(frame)
    return pre, frames


async def _warm_up(executed: list[str]) -> None:
    """Run the step once unguarded: the first model call in a process pays one-time
    costs (executor thread start, lazy imports) that can exceed a sub-second window."""
    await _guarded_frames(stall_s=0)
    executed.clear()


async def test_a_step_that_keeps_working_outlives_the_stall_window(quiet_state, busy_step):
    await _warm_up(busy_step)
    pre, frames = await _guarded_frames()

    assert len(busy_step) == ROUNDS  # every tool round ran — nothing was cut off
    assert pre.handled and frames[-1][0] == "done" and "FINAL ANSWER" in frames[-1][1]
    progress = [p for k, p in frames if k == "progress"]
    assert len(progress) >= ROUNDS  # a liveness frame per super-step
    assert all(p == {"id": "workflow:brief", "subagent": PROBE} for p in progress)
    # Liveness frames are not cards: the visible frames are exactly the old ones.
    assert [(k, p["id"]) for k, p in frames[:-1] if k != "progress"] == [
        ("tool_start", "workflow:brief"),
        ("tool_start", "workflow:brief:gather"),
        ("tool_end", "workflow:brief:gather"),
        ("tool_end", "workflow:brief"),
    ]


async def test_liveness_frames_are_rate_limited(quiet_state, busy_step, monkeypatch):
    monkeypatch.setattr(chat_dispatch, "_WORKFLOW_PROGRESS_MIN_INTERVAL_S", 3600.0, raising=False)
    _pre, frames = await _guarded_frames(stall_s=0)  # guard off: count frames only

    assert len(busy_step) == ROUNDS
    assert sum(1 for k, _ in frames if k == "progress") == 1


async def test_a_step_wedged_in_one_call_is_still_ended_by_the_stall_guard(quiet_state, monkeypatch):
    """The bound on a genuinely hung step: no super-step completes, so no liveness frame
    is sent, the guard trips, and the runner is cancelled with the turn."""
    outcome: list[str] = []

    async def run(name, inputs, *, on_step=None):
        await on_step({"phase": "start", "step_id": "gather", "subagent": PROBE})
        try:
            await asyncio.sleep(3600)  # one call that never returns
        except asyncio.CancelledError:
            outcome.append("cancelled")
            raise
        return "unreachable"

    monkeypatch.setattr(chat_commands, "_run_parsed_workflow", run)

    with pytest.raises(TurnStalled):
        await _guarded_frames(stall_s=0.2)
    assert outcome == ["cancelled"]


async def test_progress_outside_a_workflow_scope_is_a_no_op():
    from graph.subagent_progress import note_progress, progress_scope

    note_progress("researcher")  # no listener bound: nothing happens, nothing raises
    heard: list[str] = []
    with progress_scope(heard.append):
        note_progress("researcher")
    note_progress("researcher")
    assert heard == ["researcher"]

    def _broken(_t):
        raise RuntimeError("listener bug")

    with progress_scope(_broken):
        note_progress("researcher")  # a broken listener never breaks the run


# ── the settle wait cancelled before the runner stops (#3940 item 2) ─────────


async def test_a_cancelled_settle_wait_still_retrieves_the_runners_late_failure():
    loop = asyncio.get_running_loop()
    reported: list[str] = []
    previous = loop.get_exception_handler()
    loop.set_exception_handler(lambda _loop, ctx: reported.append(str(ctx.get("message", ""))))
    try:
        started = asyncio.Event()

        async def stubborn():
            try:
                started.set()
                await asyncio.sleep(3600)
            except asyncio.CancelledError:
                await asyncio.sleep(0.1)  # outlives the (cancelled) settle wait
                raise RuntimeError("late failure") from None

        runner = asyncio.create_task(stubborn())
        await started.wait()
        # The stall guard's 5s close bound cancelling the 10s settle wait, in miniature.
        stopper = asyncio.create_task(chat_dispatch._stop_abandoned_workflow(runner, "brief"))
        await asyncio.sleep(0.02)
        stopper.cancel()
        with pytest.raises(asyncio.CancelledError):
            await stopper
        await asyncio.sleep(0.2)
        assert runner.done() and not runner.cancelled()
        del runner, stopper
        gc.collect()
    finally:
        loop.set_exception_handler(previous)
    assert not [m for m in reported if "never retrieved" in m], reported


# ── a failed step ends the turn FAILED (#3957) ────────────────────────────────


@pytest.fixture
def failing_workflow(monkeypatch):
    """The real ``_run_parsed_workflow`` over a runner whose last step failed — the
    engine records the failure inline, so the output IS the step's error text."""

    async def workflow_run(name, inputs, on_step=None):
        return {
            "output": "Error: step 'brief' raised RuntimeError: provider 500",
            "steps": {"gather": "notes", "brief": "Error: step 'brief' raised RuntimeError: provider 500"},
            "failed": ["brief"],
            "degraded": [],
        }

    monkeypatch.setattr(STATE, "workflow_run", workflow_run, raising=False)


async def test_a_workflow_with_a_failed_step_ends_the_turn_failed(quiet_state, failing_workflow):
    pre = chat_dispatch._PreTurn("/brief x")
    frames = [f async for f in chat_dispatch._pre_turn_dispatch(pre, "s-fail", None)]

    kind, text = frames[-1]
    assert pre.handled and kind == "error", frames[-1]
    assert "provider 500" in text and "failed steps: brief" in text  # nothing lost


async def test_a_workflow_that_succeeds_still_completes(quiet_state, monkeypatch):
    async def workflow_run(name, inputs, on_step=None):
        return {"output": "the brief", "steps": {"brief": "the brief"}, "failed": [], "degraded": []}

    monkeypatch.setattr(STATE, "workflow_run", workflow_run, raising=False)
    pre = chat_dispatch._PreTurn("/brief x")
    frames = [f async for f in chat_dispatch._pre_turn_dispatch(pre, "s-ok", None)]
    assert frames[-1] == ("done", "the brief")


async def test_the_a2a_task_for_a_failed_workflow_is_failed(quiet_state, failing_workflow):
    """End to end through the executor: the task's terminal state is FAILED and its
    status message carries the reply text, not a COMPLETED answer of "Error: …"."""
    from a2a.server.events.event_queue import EventQueueLegacy as EventQueue
    from a2a.types import TaskState

    from a2a_impl.executor import ProtoAgentExecutor, set_terminal_hook
    from tests.test_a2a_executor_stream_close import _RecordingQueue, _request_context

    async def stream(text, context_id, **_kw):
        pre = chat_dispatch._PreTurn("/brief x")
        async for frame in chat_dispatch._pre_turn_dispatch(pre, context_id, None):
            yield frame

    outcomes = []
    set_terminal_hook(outcomes.append)
    try:
        queue = _RecordingQueue()
        await ProtoAgentExecutor(stream, stall_timeout_provider=lambda: 30.0).execute(_request_context(), queue)
    finally:
        set_terminal_hook(None)
    del EventQueue

    assert queue.terminal == [TaskState.TASK_STATE_FAILED]
    assert [o.state for o in outcomes] == ["failed"]
    assert "provider 500" in outcomes[0].error and "failed steps: brief" in outcomes[0].error
