"""Operator consent is required before a lead agent schedules its restart."""

import asyncio
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command

from graph.middleware.subagent_fence import _SCRATCHPAD_KEY, SubagentFenceMiddleware, _answer_tools
from server.turn_control import _AUTONOMOUS_HITL_SENTINEL
from tools.lg_tools import HITL_TOOL_NAMES, SUBAGENT_DENIED_TOOLS, get_all_tools
from tools.restart_tools import build_restart_tools


@pytest.mark.asyncio
@pytest.mark.parametrize("decision", [True, "approve", {"approved": True}, {"decision": "approve"}])
async def test_approve_schedules_on_running_loop(monkeypatch, decision):
    loop = asyncio.get_running_loop()

    def schedule(reason):
        assert asyncio.get_running_loop() is loop
        return {"scheduled": True}

    scheduler = Mock(side_effect=schedule)
    approval = Mock(return_value=decision)
    monkeypatch.setattr("tools.restart_tools.interrupt", approval)
    tool = build_restart_tools(scheduler)[0]
    result = await tool.ainvoke({"reason": "Plugin updated"})
    scheduler.assert_called_once_with("Plugin updated")
    assert approval.call_args.args[0] == {
        "kind": "approval",
        "title": "Approve agent restart?",
        "detail": "Plugin updated",
        "session_allow": False,
    }
    assert result == "Restart scheduled: it happens once this turn ends. End your turn now."


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "decision",
    [False, "decline", {"approved": False}, _AUTONOMOUS_HITL_SENTINEL, None, {}, "proceed", 1],
)
async def test_without_explicit_approval_does_not_schedule(monkeypatch, decision):
    scheduler = Mock()
    monkeypatch.setattr("tools.restart_tools.interrupt", lambda card: decision)
    result = await build_restart_tools(scheduler)[0].ainvoke({"reason": "Plugin updated"})
    assert result.startswith("Not restarted: ")
    scheduler.assert_not_called()


@pytest.mark.asyncio
async def test_scheduler_refusal_returns_reason(monkeypatch):
    scheduler = Mock(return_value={"scheduled": False, "reason": "Start under a supervisor."})
    monkeypatch.setattr("tools.restart_tools.interrupt", lambda card: "approve")
    result = await build_restart_tools(scheduler)[0].ainvoke({"reason": "Plugin updated"})
    assert result == "Start under a supervisor."
    scheduler.assert_called_once_with("Plugin updated")


def test_no_scheduler_no_tool():
    assert build_restart_tools(None) == []
    assert "self_restart" not in {tool.name for tool in get_all_tools()}


def _resumed_parked_request(name: str, fence: list[str]):
    """A tool call for ``name``, parked and now being resumed (non-empty task resume),
    under a turn fence that excludes it."""
    scratchpad = SimpleNamespace(resume={"interrupt-id": "approve"})
    runtime = SimpleNamespace(config={"configurable": {_SCRATCHPAD_KEY: scratchpad}})
    return SimpleNamespace(
        state={"subagent_fence": fence},
        tool_call={"name": name, "id": "call-1"},
        runtime=runtime,
    )


def test_self_restart_is_not_an_answer_type_fence_tool():
    # The crux of #4577 review: self_restart is approval-type, so the fence must NOT treat it
    # as an answer-type tool. Otherwise a resumer whose fence excludes it would run a process
    # restart instead of getting the decline. ask_human/request_user_input stay answer-type.
    assert "self_restart" not in _answer_tools()
    assert _answer_tools() == HITL_TOOL_NAMES
    assert "self_restart" in SUBAGENT_DENIED_TOOLS


def test_fence_declines_a_resumed_parked_self_restart_outside_the_fence():
    mw = SubagentFenceMiddleware()
    req = _resumed_parked_request("self_restart", fence=["current_time"])
    declined = mw._declined(req)
    assert declined is not None  # an approval resumed outside its fence is DECLINED, not run
    assert "declined" in declined.content.lower() and "not run" in declined.content.lower()


def test_fence_still_completes_a_resumed_parked_answer_tool_outside_the_fence():
    # Contrast: an answer-type tool resumed outside the fence still completes (its answer must
    # not be dropped). This proves the split didn't break the answer-type exemption.
    mw = SubagentFenceMiddleware()
    req = _resumed_parked_request("ask_human", fence=["current_time"])
    assert mw._declined(req) is None
    assert mw._deny_reason(req) is None


@pytest.mark.asyncio
@pytest.mark.parametrize("decision", ["approve", "decline", _AUTONOMOUS_HITL_SENTINEL])
async def test_real_interrupt_waits_for_resume(decision):
    scheduler = Mock(return_value={"scheduled": True})
    tool = build_restart_tools(scheduler)[0]

    async def restart(state):
        return {"result": await tool.ainvoke({"reason": "Plugin updated"})}

    builder = StateGraph(dict)
    builder.add_node("restart", restart)
    builder.add_edge(START, "restart")
    builder.add_edge("restart", END)
    graph = builder.compile(checkpointer=InMemorySaver())
    config = {"configurable": {"thread_id": "restart-test"}}
    paused = await graph.ainvoke({}, config)
    assert paused["__interrupt__"][0].value["kind"] == "approval"
    scheduler.assert_not_called()
    resumed = await graph.ainvoke(Command(resume=decision), config)
    if decision == "approve":
        scheduler.assert_called_once_with("Plugin updated")
        assert resumed["result"].startswith("Restart scheduled:")
    else:
        scheduler.assert_not_called()
        assert resumed["result"].startswith("Not restarted:")
