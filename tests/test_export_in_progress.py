"""Exports read committed checkpoints promptly while a turn holds the lock (#4652)."""

import asyncio
from copy import deepcopy
from types import SimpleNamespace

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from graph.export_op import export_thread
from server import chat_session_ops, turn_control


class FakeGraph:
    def __init__(self, messages, lock=None):
        self.messages = messages
        self.lock = lock

    async def aget_state(self, config):
        assert config == {"configurable": {"thread_id": "export-thread"}}
        if self.lock is not None:
            assert self.lock.locked()
        return SimpleNamespace(values={"messages": self.messages})


@pytest.mark.asyncio
@pytest.mark.parametrize("partial", [False, True])
async def test_running_export_returns_without_waiting(monkeypatch, partial):
    lock = asyncio.Lock()
    held, release = asyncio.Event(), asyncio.Event()
    messages = [
        HumanMessage(content="hello"),
        AIMessage(
            content=[{"type": "text", "text": "Working"},
                     {"type": "tool_use", "id": "pending", "name": "unfinished", "input": {"value": "hidden"}}],
            tool_calls=[
                {"id": "done", "name": "completed", "args": {}},
                {"id": "pending", "name": "unfinished", "args": {"value": "hidden"}},
            ],
        ),
    ]
    if partial:
        messages.append(ToolMessage(content="API_KEY=secretvalue", tool_call_id="done"))
    original = deepcopy(messages)
    monkeypatch.setattr(chat_session_ops.STATE, "graph", FakeGraph(messages, lock))
    monkeypatch.setattr(chat_session_ops.STATE, "checkpointer", object())
    monkeypatch.setattr(turn_control, "_resolve_thread_id", lambda md, sid: "export-thread")
    monkeypatch.setattr(turn_control, "_thread_lock", lambda tid: lock)

    async def running_turn():
        async with lock:
            held.set()
            await release.wait()

    task = asyncio.create_task(running_turn())
    try:
        await held.wait()
        result = await asyncio.wait_for(chat_session_ops.export_session("session"), timeout=0.5)
        assert lock.locked()
        assert result["in_progress"] is True
        assert "turn is still running" in result["message"]
        assert result["markdown"].endswith("_(turn still running…)_\n")
        assert "unfinished" not in result["markdown"]
        assert "hidden" not in result["markdown"]
        assert "_[tool_use]_" not in result["markdown"]
        assert "Working" in result["markdown"]
        assert ("completed" in result["markdown"]) is partial
        if partial:
            assert "secretvalue" not in result["markdown"]
            assert "secret-assignment" in result["redactions"]
        assert messages == original
    finally:
        release.set()
        await task


@pytest.mark.asyncio
async def test_idle_export_keeps_locked_read_and_existing_rendering(monkeypatch):
    lock = asyncio.Lock()
    messages = [HumanMessage(content="hello"), AIMessage(content="hi")]
    monkeypatch.setattr(chat_session_ops.STATE, "graph", FakeGraph(messages, lock))
    monkeypatch.setattr(chat_session_ops.STATE, "checkpointer", object())
    monkeypatch.setattr(turn_control, "_resolve_thread_id", lambda md, sid: "export-thread")
    monkeypatch.setattr(turn_control, "_thread_lock", lambda tid: lock)
    result = await chat_session_ops.export_session("session")
    assert result["in_progress"] is False
    assert not lock.locked()
    assert result["message"] == "Exported 2 message(s) as Markdown."
    assert "still running" not in result["markdown"]
    assert "hi" in result["markdown"]


@pytest.mark.asyncio
async def test_filter_preserves_complete_batches_and_is_opt_in():
    call = AIMessage(content="", tool_calls=[{"id": "one", "name": "lookup", "args": {}}])
    graph = FakeGraph([call])
    default = await export_thread(graph, object(), "export-thread")
    assert "lookup" in default["markdown"]
    graph.messages.append(ToolMessage(content="found", tool_call_id="one"))
    complete = await export_thread(graph, object(), "export-thread", drop_unanswered_tool_calls=True)
    assert "lookup" in complete["markdown"]
    assert "found" in complete["markdown"]
    assert "still running" not in complete["markdown"]


@pytest.mark.asyncio
async def test_setup_always_has_progress_flag(monkeypatch):
    monkeypatch.setattr(chat_session_ops.STATE, "graph", None)
    result = await chat_session_ops.export_session("session")
    assert result["in_progress"] is False


@pytest.mark.asyncio
@pytest.mark.parametrize("checkpointer, reason", [(None, "no_checkpointer"), (object(), "empty_thread")])
async def test_running_empty_export_keeps_progress_flag(monkeypatch, checkpointer, reason):
    lock = asyncio.Lock()
    monkeypatch.setattr(chat_session_ops.STATE, "graph", FakeGraph([], lock))
    monkeypatch.setattr(chat_session_ops.STATE, "checkpointer", checkpointer)
    monkeypatch.setattr(turn_control, "_resolve_thread_id", lambda md, sid: "export-thread")
    monkeypatch.setattr(turn_control, "_thread_lock", lambda tid: lock)
    async with lock:
        result = await asyncio.wait_for(chat_session_ops.export_session("session"), timeout=0.5)
    assert result["in_progress"] is True
    assert result["reason"] == reason
    assert "turn is still running" in result["message"]


@pytest.mark.asyncio
async def test_dict_messages_match_tool_results_by_id():
    messages = [
        {"role": "assistant", "content": "Working", "tool_calls": [
            {"id": "one", "name": "finished", "args": {}},
            {"id": "two", "name": "pending", "args": {}},
        ]},
        {"role": "tool", "tool_call_id": "one", "content": "done"},
    ]
    original = deepcopy(messages)
    result = await export_thread(FakeGraph(messages), object(), "export-thread", drop_unanswered_tool_calls=True)
    assert "finished" in result["markdown"]
    assert "pending" not in result["markdown"]
    assert "done" in result["markdown"]
    assert messages == original
