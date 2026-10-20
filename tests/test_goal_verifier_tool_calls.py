"""The goal ``llm`` verifier sees the turn's tool calls (launch bug B2).

Nothing built ``VerifyContext.tool_summary``: ``GoalDrive`` called
``evaluate(last_text=…)`` only, so the judge's prompt always read
``Recent tool calls: (none)``. A goal proven by tool results — write a haiku to the shared
note, read it back, confirm three lines — was judged "no tool calls are visible" on every
iteration until the budget ran out and it was marked unachievable.

These tests use real LangChain message objects (``AIMessage.tool_calls`` +
``ToolMessage``) and a real checkpointed LangGraph thread.
"""

from __future__ import annotations

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, MessagesState, StateGraph

from graph.config import LangGraphConfig
from graph.goals.controller import GoalController
from graph.goals.store import GoalStore
from graph.goals.transcript import summarize_tool_calls

HAIKU = "Servers hum awake\nthe launch post climbs the front page\ncoffee, then more logs"


def _haiku_turn(kickoff: str = "GOAL: write a haiku into the shared note, then verify three lines") -> list:
    return [
        HumanMessage(content="an earlier turn"),
        AIMessage(content="", tool_calls=[{"id": "old", "name": "stale_tool", "args": {}}]),
        ToolMessage(content="old result", tool_call_id="old"),
        AIMessage(content="earlier answer"),
        HumanMessage(content=kickoff),
        AIMessage(
            content="Writing the haiku.",
            tool_calls=[{"id": "c1", "name": "write_note", "args": {"text": HAIKU}, "type": "tool_call"}],
        ),
        ToolMessage(content="saved shared note (3 lines)", tool_call_id="c1", name="write_note"),
        AIMessage(content="", tool_calls=[{"id": "c2", "name": "read_note", "args": {}, "type": "tool_call"}]),
        ToolMessage(content=HAIKU, tool_call_id="c2", name="read_note"),
        AIMessage(content="Done — the shared note holds the haiku, exactly three lines."),
    ]


def test_summary_pairs_each_call_with_its_result_for_the_current_turn_only():
    out = summarize_tool_calls(_haiku_turn())
    lines = out.splitlines()
    assert lines[0].startswith("- write_note(") and "Servers hum awake" in lines[0]
    assert "saved shared note (3 lines)" in lines[0]
    assert lines[1].startswith("- read_note()") and "coffee, then more logs" in lines[1]
    assert "stale_tool" not in out  # a previous turn's calls are not this turn's evidence
    # line breaks stay visible (escaped), so "exactly three lines" is judgeable from the digest
    assert "Servers hum awake\\nthe launch post climbs the front page\\ncoffee, then more logs" in lines[1]


def test_mid_turn_injected_human_messages_do_not_end_the_turn():
    msgs = _haiku_turn()
    msgs.insert(7, HumanMessage(content="note", additional_kwargs={"protoagent_guard_note": "x"}))
    msgs.insert(
        8,
        HumanMessage(content="[User message received while you were working — address it now]\n\nhi"),
    )
    out = summarize_tool_calls(msgs)
    assert "write_note" in out and "read_note" in out


def test_block_content_results_errors_and_missing_results():
    """An Anthropic-shaped turn: the AI message's content is a block list carrying the tool_use
    (``tool_calls`` mirrors it) — the call renders exactly once; a block-list tool result
    renders its text; an error status and a call with no result yet are marked."""
    msgs = [
        HumanMessage(content="go"),
        AIMessage(
            content=[
                {"type": "text", "text": "running"},
                {"type": "tool_use", "id": "t1", "name": "run", "input": {"cmd": "ls"}},
            ],
            tool_calls=[{"id": "t1", "name": "run", "args": {"cmd": "ls"}}],
        ),
        ToolMessage(content=[{"type": "text", "text": "boom"}], tool_call_id="t1", status="error"),
        AIMessage(content="", tool_calls=[{"id": "t2", "name": "slow", "args": {}}]),
    ]
    out = summarize_tool_calls(msgs)
    assert out.count("- run(") == 1
    assert '- run({"cmd": "ls"}) → [error] boom' in out
    assert "- slow() → (no result)" in out


def test_no_tool_calls_is_empty_and_output_is_bounded():
    assert summarize_tool_calls([HumanMessage(content="hi"), AIMessage(content="hello")]) == ""
    assert summarize_tool_calls(None) == ""
    many = [HumanMessage(content="go")]
    for i in range(200):
        many.append(AIMessage(content="", tool_calls=[{"id": f"c{i}", "name": "t", "args": {"x": "y" * 500}}]))
        many.append(ToolMessage(content="z" * 2000, tool_call_id=f"c{i}"))
    out = summarize_tool_calls(many)
    assert len(out) < 7000
    assert out.startswith("(… ") and "earlier call(s) omitted" in out


# ── end to end: GoalDrive → real controller → llm verifier prompt ─────────────


def _real_graph(messages: list):
    """A real compiled LangGraph on an in-memory checkpointer whose one pass appends
    ``messages`` — the thread the goal drive reads its evidence from."""

    async def _agent(state):
        return {"messages": messages}

    g = StateGraph(MessagesState)
    g.add_node("agent", _agent)
    g.add_edge(START, "agent")
    g.add_edge("agent", END)
    return g.compile(checkpointer=InMemorySaver())


@pytest.mark.asyncio
async def test_goal_drive_hands_the_llm_verifier_the_turns_tool_calls(tmp_path, monkeypatch):
    import runtime.state as rs
    from server import goal_loop

    cfg = LangGraphConfig()
    ctrl = GoalController(cfg, GoalStore(tmp_path))
    await ctrl.parse_control(
        "/goal Write a haiku about launch day into the shared note, then verify the note contains exactly three lines",
        "s1",
    )
    assert ctrl.active_goal("s1") is not None

    prompts: list[str] = []

    class _Resp:
        content = '{"met": true, "reason": "read_note shows the three-line haiku"}'

    class _LLM:
        async def ainvoke(self, msgs, config=None):
            prompts.append(msgs[-1].content)
            return _Resp()

    monkeypatch.setattr("graph.llm.create_llm", lambda config, model_name=None: _LLM())

    turn = _haiku_turn()
    graph = _real_graph(turn[1:])  # the pass's output; its input is the first human message
    config = {"configurable": {"thread_id": "a2a:s1"}}
    await graph.ainvoke({"messages": [turn[0]]}, config)

    monkeypatch.setattr(rs.STATE, "graph", graph, raising=False)
    monkeypatch.setattr(rs.STATE, "goal_controller", ctrl, raising=False)
    monkeypatch.setattr(rs.STATE, "graph_config", cfg, raising=False)

    drive = goal_loop.GoalDrive("s1", config, turn[-1].content)
    steps = [s async for s in drive.steps()]

    assert len(prompts) == 1
    prompt = prompts[0]
    assert "(none)" not in prompt.split("Agent's latest message")[0]
    assert "write_note(" in prompt and "read_note()" in prompt
    assert "coffee, then more logs" in prompt  # the read-back result is visible evidence
    assert ctrl.active_goal("s1") is None  # achieved on the first evaluation
    assert any(isinstance(s, goal_loop.GoalNote) for s in steps)
