"""The ``/workflow`` short-circuit's runner ends with its turn (#3933).

``_pre_turn_dispatch`` runs a workflow command as an ``asyncio`` task and streams its step
frames. When the dispatch generator is closed early — ``aclose()`` at a frame, or the
consuming task cancelled while it waits for the next step — the runner is part of that
ending turn: it is cancelled and awaited (bounded) BEFORE the close returns, never left
running detached with nobody reading its frames. A run that completes normally is
unchanged.
"""

from __future__ import annotations

import asyncio
import logging
import types

import pytest

import server.chat_commands as chat_commands
import server.chat_dispatch as chat_dispatch
from runtime.state import STATE


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


class _Workflow:
    """A fake workflow run: emits one step start, then blocks until released or cancelled."""

    def __init__(self, *, swallow_cancel: bool = False):
        self.outcome: list[str] = []
        self.release = asyncio.Event()
        self.blocked = asyncio.Event()
        self.swallow_cancel = swallow_cancel

    async def run(self, name, inputs, *, on_step=None):
        await on_step({"phase": "start", "step_id": "gather", "subagent": "researcher"})
        try:
            self.blocked.set()
            await self.release.wait()
        except asyncio.CancelledError:
            self.outcome.append("cancelled")
            if self.swallow_cancel:
                await asyncio.sleep(0.3)  # a slow step that outlives the settle bound
            raise
        await on_step({"phase": "end", "step_id": "gather", "output": "found"})
        self.outcome.append("finished")
        return f"wf:{name}"


def _dispatch():
    pre = chat_dispatch._PreTurn("/brief x")
    return pre, chat_dispatch._pre_turn_dispatch(pre, "s-wf", None)


def _live_tasks() -> list[asyncio.Task]:
    """Every unfinished task on the loop other than the test's own — a live workflow
    runner shows up here."""
    me = asyncio.current_task()
    return [t for t in asyncio.all_tasks() if t is not me and not t.done()]


async def test_closing_the_dispatch_after_the_first_frame_cancels_the_runner(quiet_state, monkeypatch):
    wf = _Workflow()
    monkeypatch.setattr(chat_commands, "_run_parsed_workflow", wf.run)
    _pre, gen = _dispatch()

    first = await gen.__anext__()
    assert first[0] == "tool_start" and first[1]["id"] == "workflow:brief"
    assert _live_tasks()  # the runner is in flight
    await gen.aclose()

    # Stopped BEFORE aclose() returned — not orphaned, not left for GC. (Closed this
    # early, the cancel lands before the run's first step, so it never starts at all.)
    assert _live_tasks() == []
    await asyncio.sleep(0.05)
    assert not wf.blocked.is_set() and "finished" not in wf.outcome


async def test_closing_mid_step_cancels_the_runner(quiet_state, monkeypatch):
    wf = _Workflow()
    monkeypatch.setattr(chat_commands, "_run_parsed_workflow", wf.run)
    _pre, gen = _dispatch()

    await gen.__anext__()  # umbrella card
    step = await gen.__anext__()  # the step's own card
    assert step[1]["id"] == "workflow:brief:gather"
    await gen.aclose()

    assert wf.outcome == ["cancelled"]
    assert _live_tasks() == []


async def test_cancelling_the_consumer_while_it_waits_for_a_step_cancels_the_runner(quiet_state, monkeypatch):
    wf = _Workflow()
    monkeypatch.setattr(chat_commands, "_run_parsed_workflow", wf.run)
    _pre, gen = _dispatch()

    async def _consume():
        async for _ in gen:
            pass

    consumer = asyncio.create_task(_consume())
    await wf.blocked.wait()
    await asyncio.sleep(0)  # the consumer is now parked in the step-queue get()
    consumer.cancel()
    with pytest.raises(asyncio.CancelledError):
        await consumer

    assert wf.outcome == ["cancelled"]
    assert _live_tasks() == []


async def test_a_runner_that_ignores_the_cancel_is_released_after_the_settle_bound(quiet_state, monkeypatch, caplog):
    wf = _Workflow(swallow_cancel=True)
    monkeypatch.setattr(chat_commands, "_run_parsed_workflow", wf.run)
    monkeypatch.setattr(chat_dispatch, "_WORKFLOW_CANCEL_SETTLE_S", 0.05)
    _pre, gen = _dispatch()

    await gen.__anext__()
    await gen.__anext__()  # the step card: the run is inside its step now
    with caplog.at_level(logging.WARNING, logger="protoagent.server"):
        await asyncio.wait_for(gen.aclose(), 5.0)  # bounded: the close does not hang

    assert wf.outcome == ["cancelled"]
    assert any("did not stop within" in r.getMessage() for r in caplog.records)
    await asyncio.sleep(0.4)  # let the dropped runner finish inside this test's loop


async def test_a_workflow_that_completes_streams_every_frame_unchanged(quiet_state, monkeypatch):
    wf = _Workflow()
    wf.release.set()
    monkeypatch.setattr(chat_commands, "_run_parsed_workflow", wf.run)
    monkeypatch.setattr(chat_commands, "_parse_workflow_command", lambda m: ("brief", {}))
    pre, gen = _dispatch()

    frames = [f async for f in gen]

    assert wf.outcome == ["finished"]
    assert [(k, p["id"]) if isinstance(p, dict) else (k, p) for k, p in frames] == [
        ("tool_start", "workflow:brief"),
        ("tool_start", "workflow:brief:gather"),
        ("tool_end", "workflow:brief:gather"),
        ("tool_end", "workflow:brief"),
        ("done", "wf:brief"),
    ]
    assert pre.handled
