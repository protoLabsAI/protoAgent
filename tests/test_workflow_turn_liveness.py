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
import time
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
    monkeypatch.setattr(chat_dispatch, "_PROGRESS_MIN_INTERVAL_S", 0.0, raising=False)


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
    monkeypatch.setattr(chat_dispatch, "_PROGRESS_MIN_INTERVAL_S", 3600.0, raising=False)
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
    from graph.turn_liveness import note_progress, progress_scope

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


async def test_progress_reported_from_another_thread_reaches_the_turn(quiet_state, monkeypatch):
    """`asyncio.Queue` isn't thread-safe: a subagent driven on another thread must report
    through the owning loop (`call_soon_threadsafe`). A bare `put_nowait` from a thread
    queues the wakeup without waking the loop, so the turn doesn't see the frame until
    something else wakes it — here, not until the reporting thread is done."""
    import threading

    from graph.turn_liveness import note_progress

    seen = threading.Event()

    async def run(name, inputs, *, on_step=None):
        await on_step({"phase": "start", "step_id": "gather", "subagent": PROBE})

        def _report() -> bool:
            time.sleep(0.1)  # the turn is parked on its step queue by now
            note_progress(PROBE)
            return seen.wait(2.0)  # the turn must see it WHILE this thread is still busy

        delivered = await asyncio.to_thread(_report)
        await on_step({"phase": "end", "step_id": "gather", "output": "ok"})
        return "ok" if delivered else "late"

    monkeypatch.setattr(chat_commands, "_run_parsed_workflow", run)

    pre = chat_dispatch._PreTurn("/brief x")
    frames = []
    async for frame in chat_dispatch._pre_turn_dispatch(pre, "s-thread", None):
        frames.append(frame)
        if frame[0] == "progress":
            seen.set()

    assert ("progress", {"id": "workflow:brief", "subagent": PROBE}) in frames
    assert frames[-1] == ("done", "ok"), "the progress frame only arrived after the thread ended"


# ── a stall is a failure, a cancel is a cancel — in the run store (#3940 L1) ──


def _blocking_plugin_run(monkeypatch, store):
    """STATE.workflow_run → the REAL workflows plugin run path, one step that blocks."""
    import plugins.workflows as wf
    from tests.test_workflow_run_state import _FakeReg, _patch_sdk

    async def run_subagent(subagent_type, prompt, description=""):
        await asyncio.sleep(3600)

    _patch_sdk(monkeypatch, run_subagent)

    async def workflow_run(name, inputs, on_step=None):
        return await wf._execute(_FakeReg(), "demo", {"topic": "ai"}, on_step=on_step, run_store=store)

    monkeypatch.setattr(STATE, "workflow_run", workflow_run, raising=False)
    monkeypatch.setattr(chat_commands, "_run_parsed_workflow", chat_commands._run_parsed_workflow)


async def test_a_stalled_workflow_run_is_recorded_failed_with_the_reason(quiet_state, monkeypatch, tmp_path):
    from plugins.workflows.run_state import WorkflowRunStore

    store = WorkflowRunStore(tmp_path)
    _blocking_plugin_run(monkeypatch, store)

    with pytest.raises(TurnStalled):
        await _guarded_frames(stall_s=0.3)

    state = store.load(store.run_id)
    assert state["status"] == "failed"
    assert state["error"].startswith("The turn stalled: no progress for 0.3s")
    assert state["step_meta"]["gather"]["status"] == "failed"


async def test_a_cancelled_workflow_turn_is_recorded_cancelled(quiet_state, monkeypatch, tmp_path):
    """An operator's CancelTask cancels the producer: the guard records no reason."""
    from plugins.workflows.run_state import WorkflowRunStore

    store = WorkflowRunStore(tmp_path)
    _blocking_plugin_run(monkeypatch, store)

    consumer = asyncio.create_task(_guarded_frames(stall_s=30))
    for _ in range(100):
        await asyncio.sleep(0.01)
        if store.run_id and (store.load(store.run_id) or {}).get("step_meta", {}).get("gather"):
            break
    consumer.cancel()
    with pytest.raises(asyncio.CancelledError):
        await consumer

    state = store.load(store.run_id)
    assert state["status"] == "cancelled" and "error" not in state


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


# ── a failed run ends the turn FAILED, a partial success COMPLETES (#3957, #3940) ──
#
# The rule: the turn fails only when a step the final output is rendered from failed, or
# every step did. The engine keeps the DAG going past a failed branch on purpose, so a
# later step can still deliver. These run the REAL engine (`execute_workflow`) behind the
# real `_run_parsed_workflow`; only the step runner is scripted.


def _engine_run(recipe: dict, failing: set[str]):
    from plugins.workflows.engine import execute_workflow

    async def run_step(subagent, prompt, step_id):
        if step_id in failing:
            raise RuntimeError(f"provider 500 in {step_id}")
        return f"<{step_id} ok>"

    async def workflow_run(name, inputs, on_step=None):
        return await execute_workflow(recipe, {}, run_step=run_step)

    return workflow_run


_FAN_IN = {
    "name": "brief",
    "steps": [
        {"id": "angle_a", "subagent": "researcher", "prompt": "a"},
        {"id": "angle_b", "subagent": "researcher", "prompt": "b"},
        {
            "id": "synth",
            "subagent": "researcher",
            "depends_on": ["angle_a", "angle_b"],
            "prompt": "{{steps.angle_a.output}} {{steps.angle_b.output}}",
        },
    ],
    "output": "{{steps.synth.output}}",
}

_STATIC_OUTPUT = {
    "name": "brief",
    "steps": [
        {"id": "one", "subagent": "researcher", "prompt": "1"},
        {"id": "two", "subagent": "researcher", "prompt": "2"},
    ],
    "output": "Report filed.",
}


async def _frames_for(monkeypatch, recipe: dict, failing: set[str]):
    monkeypatch.setattr(STATE, "workflow_run", _engine_run(recipe, failing), raising=False)
    pre = chat_dispatch._PreTurn("/brief x")
    frames = [f async for f in chat_dispatch._pre_turn_dispatch(pre, "s-rule", None)]
    assert pre.handled
    return frames


async def test_a_failed_non_final_branch_still_completes_with_the_note(quiet_state, monkeypatch):
    frames = await _frames_for(monkeypatch, _FAN_IN, {"angle_a"})

    kind, text = frames[-1]
    assert kind == "done", frames[-1]
    assert text.startswith("<synth ok>") and "failed steps: angle_a" in text


async def test_a_failed_output_step_fails_the_turn_and_keeps_the_output(quiet_state, monkeypatch):
    frames = await _frames_for(monkeypatch, _FAN_IN, {"synth"})

    (text_kind, output), (kind, error) = frames[-2:]
    assert kind == "error" and error == "workflow /brief failed: step(s) synth"  # one short line
    assert text_kind == "text" and "provider 500 in synth" in output and "failed steps: synth" in output


async def test_every_step_failing_fails_the_turn_even_with_a_static_output(quiet_state, monkeypatch):
    frames = await _frames_for(monkeypatch, _STATIC_OUTPUT, {"one", "two"})

    assert frames[-1] == ("error", "workflow /brief failed: step(s) one, two")
    assert frames[-2][0] == "text" and frames[-2][1].startswith("Report filed.")


async def test_one_failure_under_a_static_output_completes(quiet_state, monkeypatch):
    frames = await _frames_for(monkeypatch, _STATIC_OUTPUT, {"one"})
    assert frames[-1][0] == "done" and "failed steps: one" in frames[-1][1]


async def test_a_workflow_that_succeeds_still_completes(quiet_state, monkeypatch):
    frames = await _frames_for(monkeypatch, _FAN_IN, set())
    assert frames[-1] == ("done", "<synth ok>")


async def test_a_runner_without_output_failed_reads_failed_on_any_failure(quiet_state, monkeypatch):
    """An older workflows runner reports no ``output_failed`` — the safe side is failed."""

    async def workflow_run(name, inputs, on_step=None):
        return {"output": "x", "steps": {}, "failed": ["s"], "degraded": []}

    monkeypatch.setattr(STATE, "workflow_run", workflow_run, raising=False)
    pre = chat_dispatch._PreTurn("/brief x")
    frames = [f async for f in chat_dispatch._pre_turn_dispatch(pre, "s-old", None)]
    assert frames[-1] == ("error", "workflow /brief failed: step(s) s")


# ── through the executor and every surface that reads its outcome (#3940 M1) ──


async def _failed_outcome(monkeypatch):
    """Run a failed `/brief` through the REAL executor; return its TurnOutcome and the
    terminal states + artifact text it enqueued."""
    from a2a.types import TaskArtifactUpdateEvent

    from a2a_impl.executor import ProtoAgentExecutor, set_terminal_hook
    from tests.test_a2a_executor_stream_close import _RecordingQueue, _request_context

    monkeypatch.setattr(STATE, "workflow_run", _engine_run(_FAN_IN, {"synth"}), raising=False)

    async def stream(text, context_id, **_kw):
        pre = chat_dispatch._PreTurn("/brief x")
        async for frame in chat_dispatch._pre_turn_dispatch(pre, context_id, None):
            yield frame

    class _Queue(_RecordingQueue):
        def __init__(self):
            super().__init__()
            self.artifact_text = ""

        async def enqueue_event(self, event):
            if isinstance(event, TaskArtifactUpdateEvent):
                self.artifact_text += "".join(p.text for p in event.artifact.parts)
            await super().enqueue_event(event)

    outcomes = []
    set_terminal_hook(outcomes.append)
    try:
        queue = _Queue()
        await ProtoAgentExecutor(stream, stall_timeout_provider=lambda: 30.0).execute(_request_context(), queue)
    finally:
        set_terminal_hook(None)
    (outcome,) = outcomes
    return outcome, queue


async def test_the_a2a_task_fails_with_a_short_error_and_keeps_the_output(quiet_state, monkeypatch):
    from a2a.types import TaskState

    outcome, queue = await _failed_outcome(monkeypatch)

    assert queue.terminal == [TaskState.TASK_STATE_FAILED]
    assert outcome.state == "failed" and outcome.error == "workflow /brief failed: step(s) synth"
    assert "provider 500 in synth" in outcome.text  # the output survives on the outcome...
    assert "provider 500 in synth" in queue.artifact_text  # ...and as the answer artifact


async def test_a_scheduled_failed_workflow_still_delivers_its_report(quiet_state, monkeypatch):
    import dataclasses

    from events import ACTIVITY_CONTEXT
    from server import a2a
    from tests.test_scheduled_delivery import _Job, _Scheduler

    outcome, _ = await _failed_outcome(monkeypatch)
    monkeypatch.setattr(STATE, "scheduler", _Scheduler(_Job()))
    fired = dataclasses.replace(outcome, origin="scheduler", trigger="job-1", context_id=ACTIVITY_CONTEXT)

    payload = a2a._scheduled_delivery_payload(fired)

    assert payload["status"] == "failed" and "provider 500 in synth" in payload["summary"]


async def test_a_failed_workflow_on_the_activity_thread_is_posted(quiet_state, monkeypatch):
    import dataclasses

    from events import ACTIVITY_CONTEXT
    from server import a2a

    outcome, _ = await _failed_outcome(monkeypatch)
    posted: list[dict] = []

    class _Log:
        def add(self, **kw):
            posted.append(kw)

    monkeypatch.setattr(STATE, "activity_log", _Log(), raising=False)
    monkeypatch.setattr(STATE, "scheduler", None, raising=False)
    monkeypatch.setattr(a2a, "_record_a2a_telemetry", lambda o: None)
    monkeypatch.setattr(a2a._event_bus, "publish", lambda *a, **k: None)

    a2a._a2a_terminal(dataclasses.replace(outcome, origin="scheduler", trigger="job-1", context_id=ACTIVITY_CONTEXT))

    (row,) = posted
    assert row["state"] == "failed" and "provider 500 in synth" in row["text"]


@pytest.fixture
def bg_job(monkeypatch, tmp_path):
    """A real background store with one job, as `test_telemetry_bg_errors_3945` builds it."""
    from background.manager import BackgroundManager
    from background.store import BackgroundStore

    from server import a2a

    mgr = BackgroundManager(
        agent_name="a",
        invoke_url="http://127.0.0.1:7870",
        store=BackgroundStore(str(tmp_path / "background" / "jobs.db")),
        api_key="k",
        bearer_token="b",
    )
    monkeypatch.setattr(STATE, "background_mgr", mgr, raising=False)
    monkeypatch.setattr(STATE, "graph_config", LangGraphConfig(background_auto_resume=False), raising=False)
    monkeypatch.setattr(STATE, "knowledge_store", None, raising=False)
    monkeypatch.delenv("BACKGROUND_WAKE", raising=False)
    monkeypatch.setattr(a2a, "_spawn_background_wake", lambda job: None)
    monkeypatch.setattr(a2a._event_bus, "publish", lambda *a, **k: None)
    jid = mgr.store.create(
        agent_name="a", origin_session="chat-42", subagent_type="researcher", description="dig", prompt="p"
    )
    return a2a, mgr, jid


async def test_a_failed_workflow_background_job_stores_its_result(quiet_state, monkeypatch, bg_job):
    import dataclasses
    import importlib

    chat_mod = importlib.import_module("server.chat")
    a2a, mgr, jid = bg_job
    outcome, _ = await _failed_outcome(monkeypatch)

    a2a._handle_background_terminal(
        dataclasses.replace(outcome, origin="background", trigger=jid, context_id=f"background:{jid}")
    )

    job = mgr.store.get(jid)
    assert job.status == "failed" and job.error == "workflow /brief failed: step(s) synth"
    assert "provider 500 in synth" in job.result
    chat_mod._drain_background_messages("chat-42")  # don't leak the queued notice
