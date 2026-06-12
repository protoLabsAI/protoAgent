"""Dangling-tool_call repair (graph.middleware.tool_call_repair).

A thread with an assistant tool_call that never got a ToolMessage 400s at the
provider on every later turn; repair_messages drops the orphan so it's valid.
"""

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from graph.middleware.tool_call_repair import ToolCallRepairMiddleware, repair_messages


def _ai(tool_calls, content=""):
    return AIMessage(content=content, tool_calls=tool_calls)


def _call(cid, name="web_search"):
    return {"name": name, "args": {"q": "x"}, "id": cid, "type": "tool_call"}


def test_healthy_history_is_untouched():
    msgs = [
        HumanMessage(content="hi"),
        _ai([_call("c1")]),
        ToolMessage(content="result", tool_call_id="c1"),
        AIMessage(content="done"),
    ]
    assert repair_messages(msgs) == []  # nothing to repair
    assert ToolCallRepairMiddleware()._repair({"messages": msgs}) is None


def test_no_tool_calls_at_all_is_noop():
    msgs = [HumanMessage(content="hi"), AIMessage(content="hello")]
    assert repair_messages(msgs) == []


def test_dangling_tool_call_is_dropped():
    orphan = _ai([_call("c1")])  # no ToolMessage answers c1
    msgs = [HumanMessage(content="smoke test execute_code"), orphan, HumanMessage(content="huh?")]
    repairs = repair_messages(msgs)
    assert len(repairs) == 1
    fixed = repairs[0]
    assert fixed.id == orphan.id          # replaces the orphan in place (same id)
    assert fixed.tool_calls == []         # the dangling call is gone
    assert fixed.content                  # pure-tool_call message gets placeholder text


def test_keeps_answered_drops_only_dangling():
    msgs = [
        _ai([_call("c1"), _call("c2")], content="calling two"),
        ToolMessage(content="r1", tool_call_id="c1"),  # c1 answered, c2 dangling
        HumanMessage(content="next"),
    ]
    repairs = repair_messages(msgs)
    assert len(repairs) == 1
    kept_ids = [tc["id"] for tc in repairs[0].tool_calls]
    assert kept_ids == ["c1"]             # answered call kept, dangling c2 dropped
    assert repairs[0].content == "calling two"


def test_repair_applied_through_the_langgraph_reducer():
    """The integration that actually matters: the middleware returns replacement
    messages, and LangGraph's `add_messages` reducer (what create_agent uses for
    the messages key) must REPLACE the orphaned assistant message in place by id —
    so the merged history the model sees has no dangling tool_call."""
    from langgraph.graph.message import add_messages

    # Build the history the way the graph does — add_messages assigns ids, which is
    # what makes the in-place replace work (a message returned with an existing id
    # REPLACES rather than appends).
    history = add_messages(
        [],
        [HumanMessage(content="smoke test execute_code"), _ai([_call("c1")]), HumanMessage(content="huh?")],
    )
    assert all(m.id for m in history)  # state messages always carry ids

    update = ToolCallRepairMiddleware()._repair({"messages": history})
    assert update is not None

    merged = add_messages(history, update["messages"])
    # Same count + order — the orphan was REPLACED in place, not appended.
    assert len(merged) == len(history)
    ai = [m for m in merged if isinstance(m, AIMessage)]
    assert len(ai) == 1 and ai[0].tool_calls == []  # the dangling call is gone
    # Nothing in the merged history is left with an unanswered tool_call.
    answered = {m.tool_call_id for m in merged if isinstance(m, ToolMessage)}
    for m in merged:
        for tc in (getattr(m, "tool_calls", None) or []):
            assert tc["id"] in answered
