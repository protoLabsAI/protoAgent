"""The ``<working_state>`` part of the turn projection is re-read after tools run.

ADR 0108 D2 composes the projection once per turn, in ``before_agent``. Agents reported
the consequence: ``update_task`` returned success, and the next model call of the SAME
turn still showed the task at its prior status — the turn-entry snapshot — so the agent
re-did the update or doubted it. ``wrap_model_call`` now swaps in a fresh working-state
block once a tool has run this turn; every other part stays the turn's snapshot.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from langchain.agents import create_agent
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_core.tools import StructuredTool
from langgraph.checkpoint.memory import InMemorySaver

import runtime.state as rs
from graph.context_frame import is_context_frame
from graph.middleware.knowledge import KnowledgeMiddleware, refresh_working_state
from graph.state import ProtoAgentState

HOT = "HOT-FACT-ws9"


class _HotStore:
    def get_hot_memory(self, max_chars: int = 6000) -> str:
        return HOT

    def get_hot_memory_entries(self, max_chars: int = 6000) -> list[tuple[int, str]]:
        return [(1, HOT)]

    def search(self, query: str, k: int = 5, **kwargs) -> list[dict]:
        return []


class _Tasks:
    """A mutable stand-in for the tasks store; ``reads`` counts working-state reads."""

    def __init__(self):
        self.items = [{"status": "open", "id": "task-1", "priority": 1, "title": "ship the fix"}]
        self.reads = 0

    def list(self, *, include_closed=False):
        self.reads += 1
        return [dict(i) for i in self.items if include_closed or i["status"] != "closed"]


class _Model(BaseChatModel):
    """First call: update the task. Second call: finish. Records each call's frame."""

    frames: Any = None

    @property
    def _llm_type(self) -> str:
        return "ws-refresh-fake"

    def bind_tools(self, tools, **kwargs):
        return self

    def _reply(self, messages) -> AIMessage:
        self.frames.append("\n".join(str(m.content) for m in messages if is_context_frame(m)))
        if not any(isinstance(m, ToolMessage) for m in messages):
            call = {"name": "update_task", "args": {"status": "in_progress"}, "id": "c1", "type": "tool_call"}
            return AIMessage(content="", tool_calls=[call])
        return AIMessage(content="done")

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        return ChatResult(generations=[ChatGeneration(message=self._reply(messages))])

    async def _agenerate(self, messages, stop=None, run_manager=None, **kwargs):
        return ChatResult(generations=[ChatGeneration(message=self._reply(messages))])


@pytest.fixture
def tasks(monkeypatch):
    for attr in ("goal_controller", "tasks_store", "watch_controller", "scheduler"):
        monkeypatch.setattr(rs.STATE, attr, None, raising=False)
    store = _Tasks()
    monkeypatch.setattr(rs.STATE, "tasks_store", store, raising=False)
    return store


def _graph(tasks: _Tasks, frames: list[str]):
    def update_task(status: str) -> str:
        """Update task-1's status."""
        tasks.items[0]["status"] = status
        return f"Updated task-1 → {status}"

    knowledge = KnowledgeMiddleware(_HotStore())
    knowledge._prior_sessions_cache = ""
    return create_agent(
        model=_Model(frames=frames),
        tools=[StructuredTool.from_function(update_task)],
        middleware=[knowledge],
        system_prompt="sys",
        state_schema=ProtoAgentState,
        checkpointer=InMemorySaver(),
    )


def _assert_refreshed(frames: list[str], tasks: _Tasks):
    assert len(frames) == 2, frames
    first, second = frames
    assert "[open] task-1" in first
    # The point: the call right after the update sees the NEW status, not the snapshot.
    assert "[in_progress] task-1" in second, second
    assert "[open] task-1" not in second
    # The rest of the projection is the turn's snapshot, unchanged and still ahead of
    # the working state (its position in the frame is stable).
    assert HOT in second
    assert second.index(HOT) < second.index("<working_state>")
    assert second.count("<working_state>") == 1
    # Composed once at turn entry + re-read once after the tool — the first model call
    # does not re-read what the compose just read.
    assert tasks.reads == 2, tasks.reads


def test_task_updated_mid_turn_shows_new_status_on_next_call(tasks):
    frames: list[str] = []
    _graph(tasks, frames).invoke(
        {"messages": [{"role": "user", "content": "start the task"}]},
        {"configurable": {"thread_id": "t"}},
    )
    _assert_refreshed(frames, tasks)


def test_task_updated_mid_turn_shows_new_status_on_next_call_async(tasks):
    frames: list[str] = []
    asyncio.run(
        _graph(tasks, frames).ainvoke(
            {"messages": [{"role": "user", "content": "start the task"}]},
            {"configurable": {"thread_id": "t"}},
        )
    )
    _assert_refreshed(frames, tasks)


# ── refresh_working_state: the in-place swap ──────────────────────────────────────


def _ws(body: str) -> str:
    return f"<working_state>\n{body}\n</working_state>"


def _patch_block(monkeypatch, value: str):
    import graph.projection as projection

    monkeypatch.setattr(projection, "working_state_block", lambda state: value)


def test_swap_replaces_only_the_trailing_block(monkeypatch):
    mem, old, new = "<injected_memory>m</injected_memory>", _ws("old"), _ws("newer body")
    text = f"{mem}\n\n{old}"
    sections = [{"label": "Injected memory", "chars": len(mem)}, {"label": "Working state", "chars": len(old)}]
    _patch_block(monkeypatch, new)
    out, secs = refresh_working_state(text, sections, {})
    assert out == f"{mem}\n\n{new}"
    assert secs == [{"label": "Injected memory", "chars": len(mem)}, {"label": "Working state", "chars": len(new)}]


def test_unchanged_block_returns_the_same_objects(monkeypatch):
    old = _ws("same")
    text, sections = f"x\n\n{old}", [{"label": "Skills index", "chars": 1}, {"label": "Working state", "chars": len(old)}]
    _patch_block(monkeypatch, old)
    out, secs = refresh_working_state(text, sections, {})
    assert out is text and secs is sections


def test_block_that_appears_mid_turn_is_appended(monkeypatch):
    new = _ws("first task")
    _patch_block(monkeypatch, new)
    out, secs = refresh_working_state("skills", [{"label": "Skills index", "chars": 6}], {})
    assert out == f"skills\n\n{new}"
    assert secs[-1] == {"label": "Working state", "chars": len(new)}


def test_block_that_empties_mid_turn_is_removed(monkeypatch):
    old = _ws("last task")
    _patch_block(monkeypatch, "")
    out, secs = refresh_working_state(
        f"skills\n\n{old}", [{"label": "Skills index", "chars": 6}, {"label": "Working state", "chars": len(old)}], {}
    )
    assert out == "skills"
    assert secs == [{"label": "Skills index", "chars": 6}]


def test_refresh_failure_keeps_the_composed_text(monkeypatch):
    import graph.projection as projection

    def boom(state):
        raise RuntimeError("store down")

    monkeypatch.setattr(projection, "working_state_block", boom)
    sections = [{"label": "Working state", "chars": 3}]
    assert refresh_working_state("abc", sections, {}) == ("abc", sections)


def test_unannotated_text_is_left_alone_for_none_and_empty_sections(monkeypatch):
    # No section annotations means nothing to locate the old block by; appending would
    # duplicate a working state already inside the text.
    text = f"memory\n\n{_ws('old')}"
    _patch_block(monkeypatch, _ws("new"))
    for sections in (None, []):
        out, secs = refresh_working_state(text, sections, {})
        assert out is text and secs is sections


def test_block_appears_when_nothing_was_injected(monkeypatch):
    new = _ws("first task")
    _patch_block(monkeypatch, new)
    out, secs = refresh_working_state("", [], {})
    assert out == new and secs == [{"label": "Working state", "chars": len(new)}]
