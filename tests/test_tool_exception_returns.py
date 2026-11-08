"""A tool's ToolException is a failed call the model sees, never a crashed turn (graph/tool_errors).

Reproduced 2026-11-08: run_command raised ToolException (a timeout / spawn error) with no
handle_tool_error, and create_agent's ToolNode re-raised it, so the whole turn died.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from langchain.agents import create_agent
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage, ToolMessage
from langchain_core.tools import ToolException, tool

from graph.tool_errors import return_tool_exceptions


class _Model(GenericFakeChatModel):
    def bind_tools(self, tools, **kw):
        return self


def _agent(tools):
    calls = iter(
        [
            AIMessage(content="", tool_calls=[{"name": "run_command", "args": {"command": "x"}, "id": "c1"}]),
            AIMessage(content="done"),
        ]
    )
    return create_agent(_Model(messages=calls), tools=tools)


def _failing():
    @tool
    def run_command(command: str) -> str:
        """Run a command."""
        raise ToolException("command timed out after 120s")

    return run_command


def test_without_the_fix_a_tool_exception_kills_the_turn():
    """Pins the framework behaviour this module exists to change."""
    with pytest.raises(ToolException):
        _agent([_failing()]).invoke({"messages": [("user", "go")]})


def test_a_tool_exception_becomes_an_error_result_the_model_sees():
    out = _agent(return_tool_exceptions([_failing()])).invoke({"messages": [("user", "go")]})
    results = [m for m in out["messages"] if isinstance(m, ToolMessage)]
    assert len(results) == 1
    assert results[0].status == "error"
    assert "timed out" in results[0].content
    assert out["messages"][-1].content == "done"  # the turn carried on


def test_a_tools_own_handler_is_kept():
    t = _failing()
    t.handle_tool_error = lambda e: f"custom: {e}"
    return_tool_exceptions([t])
    assert callable(t.handle_tool_error)


def test_other_exceptions_still_propagate():
    @tool
    def run_command(command: str) -> str:
        """Run a command."""
        raise RuntimeError("a real bug")

    with pytest.raises(RuntimeError):
        _agent(return_tool_exceptions([run_command])).invoke({"messages": [("user", "go")]})


def test_every_final_tool_list_in_agent_build_goes_through_it():
    src = (Path(__file__).resolve().parent.parent / "src" / "graph" / "agent.py").read_text()
    lines = [ln.strip() for ln in src.splitlines() if "drop_disabled_tools(all_tools" in ln]
    assert lines and all(ln.startswith("all_tools = return_tool_exceptions(drop_disabled_tools(") for ln in lines)
