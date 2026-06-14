"""The `wait` tool (yield-and-resume) + the WaitYieldMiddleware that ends the
turn after it runs — so the agent stops busy-polling and is re-triggered by the
scheduler instead."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from graph.middleware.wait_yield import WaitYieldMiddleware, _just_waited
from tools.lg_tools import _build_scheduler_tools


# ── WaitYieldMiddleware ───────────────────────────────────────────────────────

def _tool_msg(name: str, content: str = "ok", status: str | None = None) -> ToolMessage:
    kw = {"content": content, "tool_call_id": f"c-{name}", "name": name}
    if status:
        kw["status"] = status
    return ToolMessage(**kw)


def test_just_waited_true_after_successful_wait():
    msgs = [HumanMessage("go"), AIMessage("…"), _tool_msg("wait", "Yielding for 40s …")]
    assert _just_waited(msgs) is True


def test_just_waited_false_on_fresh_turn():
    # A new stimulus is the trailing message — wait may be deep in history but
    # didn't just run.
    msgs = [_tool_msg("wait", "Yielding …"), AIMessage("done"), HumanMessage("new task")]
    assert _just_waited(msgs) is False


def test_just_waited_false_for_other_tools():
    msgs = [AIMessage("…"), _tool_msg("st_ship", "IN_TRANSIT …")]
    assert _just_waited(msgs) is False


def test_just_waited_true_with_parallel_tools():
    # wait alongside another tool in the same step still yields.
    msgs = [AIMessage("…"), _tool_msg("st_ship", "ok"), _tool_msg("wait", "Yielding …")]
    assert _just_waited(msgs) is True


def test_just_waited_false_when_wait_errored():
    msgs = [AIMessage("…"), _tool_msg("wait", "Error: couldn't schedule", status="error")]
    assert _just_waited(msgs) is False
    msgs2 = [AIMessage("…"), _tool_msg("wait", "Error: `then` is required.")]
    assert _just_waited(msgs2) is False


def test_middleware_jumps_to_end_only_after_wait():
    mw = WaitYieldMiddleware()
    waited = {"messages": [AIMessage("…"), _tool_msg("wait", "Yielding …")]}
    assert mw.before_model(waited, None) == {"jump_to": "end"}

    fresh = {"messages": [HumanMessage("hello")]}
    assert mw.before_model(fresh, None) is None


# ── the `wait` tool ───────────────────────────────────────────────────────────

class _FakeJob:
    def __init__(self, next_fire: str):
        self.id = "job-1"
        self.next_fire = next_fire


class _FakeScheduler:
    def __init__(self):
        self.added: list[tuple[str, str]] = []
        self.last_context_id: str | None = "__unset__"

    def add_job(self, prompt, schedule, *, job_id=None, timezone=None, context_id=None):
        self.added.append((prompt, schedule))
        self.last_context_id = context_id
        return _FakeJob(schedule)

    def list_jobs(self):
        return []


def _wait_tool(sched):
    return next(t for t in _build_scheduler_tools(sched) if t.name == "wait")


@pytest.mark.asyncio
async def test_wait_schedules_a_one_shot_in_the_future():
    sched = _FakeScheduler()
    out = await _wait_tool(sched).ainvoke({"seconds": 40, "then": "Dock and sell ore."})

    assert len(sched.added) == 1
    prompt, schedule = sched.added[0]
    assert prompt == "Dock and sell ore."
    fire = datetime.fromisoformat(schedule)  # parses ISO-8601 (one-shot)
    delta = (fire - datetime.now(UTC)).total_seconds()
    assert 30 < delta <= 41  # ~40s out
    assert "Dock and sell ore." in out and "re-invoked" in out


@pytest.mark.asyncio
async def test_wait_clamps_to_at_least_one_second():
    sched = _FakeScheduler()
    await _wait_tool(sched).ainvoke({"seconds": 0, "then": "continue"})
    fire = datetime.fromisoformat(sched.added[0][1])
    assert (fire - datetime.now(UTC)).total_seconds() > 0


@pytest.mark.asyncio
async def test_wait_requires_a_then_instruction():
    sched = _FakeScheduler()
    out = await _wait_tool(sched).ainvoke({"seconds": 10, "then": "  "})
    assert out.startswith("Error:")
    assert sched.added == []  # nothing scheduled


@pytest.mark.asyncio
async def test_wait_reads_session_from_injected_state():
    # The fix (bd-3b6): wait reads the originating session from the injected graph
    # state — reliable in a tool body — not the tracing contextvar, which reads
    # empty there and silently dropped the resume to the Activity thread. Passing
    # `state` directly mirrors what the ToolNode injects at runtime.
    sched = _FakeScheduler()
    await _wait_tool(sched).ainvoke(
        {"seconds": 5, "then": "continue", "state": {"session_id": "chat-xyz"}}
    )
    assert sched.last_context_id == "chat-xyz"


@pytest.mark.asyncio
async def test_wait_injected_state_wins_over_contextvar(monkeypatch):
    # State is authoritative; the contextvar is only an off-graph fallback.
    import observability.tracing as tracing
    monkeypatch.setattr(tracing, "current_session_id", lambda: "stale-ctxvar")
    sched = _FakeScheduler()
    await _wait_tool(sched).ainvoke(
        {"seconds": 5, "then": "x", "state": {"session_id": "live-state"}}
    )
    assert sched.last_context_id == "live-state"


@pytest.mark.asyncio
async def test_wait_falls_back_to_contextvar_off_graph(monkeypatch):
    # No injected state (tool used outside a graph turn): fall back to the
    # contextvar so existing off-graph callers keep working.
    import observability.tracing as tracing
    monkeypatch.setattr(tracing, "current_session_id", lambda: "chat-abc")
    sched = _FakeScheduler()
    await _wait_tool(sched).ainvoke({"seconds": 5, "then": "continue"})
    assert sched.last_context_id == "chat-abc"


@pytest.mark.asyncio
async def test_wait_without_a_session_leaves_context_id_none(monkeypatch):
    import observability.tracing as tracing
    monkeypatch.setattr(tracing, "current_session_id", lambda: "")
    sched = _FakeScheduler()
    await _wait_tool(sched).ainvoke({"seconds": 5, "then": "continue"})
    assert sched.last_context_id is None  # → scheduler falls back to Activity


# ── end-to-end: a wait in a REAL graph turn resumes in-thread (bd-3b6) ─────────
# The integration the old monkeypatch unit test could NOT catch: it patched
# current_session_id (the broken function) and so masked that the contextvar
# reads empty in a tool body. This drives a real create_agent graph whose model
# emits a wait tool call and asserts the scheduled resume carries the turn's
# session_id — proving session_id is a real state channel (state_schema=
# ProtoAgentState) and InjectedState delivers it to the tool.

class _ToolFake(GenericFakeChatModel):
    """Fake chat model that supports bind_tools (returns itself) so it can drop
    into create_agent and replay preset AIMessages, including tool calls."""

    def bind_tools(self, tools, **kwargs):
        return self


@pytest.mark.asyncio
async def test_wait_in_a_real_graph_stamps_the_turn_session(monkeypatch):
    from unittest.mock import patch

    from langchain_core.messages import HumanMessage
    from langgraph.checkpoint.memory import MemorySaver

    from graph.config import LangGraphConfig

    wait_call = AIMessage(
        content="",
        tool_calls=[{"name": "wait", "args": {"seconds": 30, "then": "resume"},
                     "id": "c1", "type": "tool_call"}],
    )
    fake = _ToolFake(messages=iter([wait_call, AIMessage(content="done")]))
    sched = _FakeScheduler()
    with patch("graph.agent.create_llm", lambda *a, **k: fake):
        from graph.agent import create_agent_graph
        graph = create_agent_graph(
            LangGraphConfig(), scheduler=sched, include_subagents=False,
            checkpointer=MemorySaver(),
        )
    await graph.ainvoke(
        {"messages": [HumanMessage("wait then resume")], "session_id": "sess-XYZ"},
        config={"configurable": {"thread_id": "t1"}},
    )
    assert sched.last_context_id == "sess-XYZ"
