"""A failed or user-cancelled `task` delegation must close its tool card as an ERROR
(X), not a green "done" wrapping an "Error:" string. The fix: the tool returns a
``ToolMessage(status="error")`` (which the on_tool_end → tool-call-v1 path reads via
``status == "error"``), instead of a plain string. Invoking the tool with a tool-call
envelope mirrors how the ToolNode runs it, so a preserved status here = a red card live.
"""

from __future__ import annotations

import asyncio

from langchain_core.messages import ToolMessage

import graph.agent as agent_mod
from graph.config import LangGraphConfig


def _task_tool():
    tools = agent_mod._build_task_tools(LangGraphConfig(), all_tools=[], background_mgr=None)
    return next(t for t in tools if t.name == "task")


def _invoke(task, **args):
    call = {"name": "task", "args": args, "id": "tc-1", "type": "tool_call"}
    return asyncio.run(task.ainvoke(call))


def test_failed_delegation_returns_error_toolmessage(monkeypatch):
    async def _boom(**kwargs):
        raise agent_mod.SubagentError("Subagent 'researcher' failed: boom")

    monkeypatch.setattr(agent_mod, "_run_subagent", _boom)
    result = _invoke(_task_tool(), description="dig", prompt="go", subagent_type="researcher")

    assert isinstance(result, ToolMessage)
    assert result.status == "error"
    assert result.tool_call_id == "tc-1"
    assert "researcher" in result.content  # the lead still gets a readable reason
    assert "Continue without its result" in result.content


def test_cancelled_delegation_returns_error_toolmessage(monkeypatch):
    from graph import delegations

    async def _cancel(**kwargs):
        raise asyncio.CancelledError()

    monkeypatch.setattr(agent_mod, "_run_subagent", _cancel)
    monkeypatch.setattr(delegations, "was_cancelled", lambda *a, **k: True)
    result = _invoke(_task_tool(), description="dig", prompt="go", subagent_type="researcher")

    assert isinstance(result, ToolMessage)
    assert result.status == "error"
    assert "cancelled by the user" in result.content


def test_successful_delegation_stays_plain_text(monkeypatch):
    async def _ok(**kwargs):
        return "[researcher completed: dig]\n\nfound it"

    monkeypatch.setattr(agent_mod, "_run_subagent", _ok)
    result = _invoke(_task_tool(), description="dig", prompt="go", subagent_type="researcher")

    # Success path is unchanged: the string is wrapped (as any tool return is), but its
    # status is NOT "error" — so the card renders a green "done", not the X.
    assert result.status != "error"
    assert "found it" in result.content
