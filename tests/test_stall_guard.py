"""StallGuardMiddleware — breaks a no-progress tool loop (#1446)."""

from __future__ import annotations

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from graph.middleware.stall_guard import NUDGE_MARK, StallGuardMiddleware, trailing_repeat

_DEFAULT_ARGS = {"project": "workspace", "command": "gh repo view"}


def _roundtrip(i: int, *, args=None, result="no default repo", tool="run_command"):
    cid = f"call_{i}"
    ai = AIMessage(
        content="",
        tool_calls=[{"name": tool, "args": args or _DEFAULT_ARGS, "id": cid, "type": "tool_call"}],
    )
    return [ai, ToolMessage(content=result, tool_call_id=cid)]


def _history(n: int, **kw):
    msgs = [HumanMessage(content="file the issue")]
    for i in range(n):
        msgs += _roundtrip(i, **kw)
    return msgs


def _mw():
    return StallGuardMiddleware(nudge_at=3, stop_at=6)


# ── nudge / stop thresholds ──────────────────────────────────────────────────


def test_below_threshold_is_noop():
    assert _mw().before_model({"messages": _history(2)}, None) is None


def test_nudge_fires_once_at_threshold():
    out = _mw().before_model({"messages": _history(3)}, None)
    assert out is not None and "jump_to" not in out
    assert out["messages"][0].content.startswith(NUDGE_MARK)


def test_nudge_does_not_refire_above_threshold():
    # 4 identical round-trips with no intervening note → past the nudge point,
    # not yet the stop point → no-op (the nudge already fired at 3).
    assert _mw().before_model({"messages": _history(4)}, None) is None


def test_stop_ends_the_turn_at_threshold():
    out = _mw().before_model({"messages": _history(6)}, None)
    assert out is not None and out.get("jump_to") == "end"
    assert isinstance(out["messages"][0], AIMessage)
    assert "loop" in out["messages"][0].content.lower()


# ── the nudge marker must not break the run it measures ──────────────────────


def test_injected_nudge_does_not_reset_the_count():
    # 3 round-trips, our nudge, then 3 more identical round-trips → a true run of 6.
    msgs = _history(3) + [HumanMessage(content=f"{NUDGE_MARK} change approach")]
    for i in range(3, 6):
        msgs += _roundtrip(i)
    out = _mw().before_model({"messages": msgs}, None)
    assert out is not None and out.get("jump_to") == "end"


def test_real_user_message_breaks_the_run():
    # A genuine user message mid-loop resets the run: only the 3 after it count.
    msgs = _history(3) + [HumanMessage(content="actually, try the other repo")]
    for i in range(3, 6):
        msgs += _roundtrip(i)
    out = _mw().before_model({"messages": msgs}, None)
    assert out is not None and out.get("jump_to") is None  # nudge, not stop
    assert out["messages"][0].content.startswith(NUDGE_MARK)


# ── only fires on a genuine stall ────────────────────────────────────────────


def test_varied_results_are_not_a_stall():
    msgs = [HumanMessage(content="go")]
    for i in range(6):
        msgs += _roundtrip(i, result=f"different result {i}")
    assert _mw().before_model({"messages": msgs}, None) is None


def test_varied_args_are_not_a_stall():
    msgs = [HumanMessage(content="go")]
    for i in range(6):
        msgs += _roundtrip(i, args={"project": "workspace", "command": f"ls dir{i}"})
    assert _mw().before_model({"messages": msgs}, None) is None


def test_fresh_user_turn_after_loop_is_noop():
    # Tail is a new HumanMessage (not a tool block) → no active loop.
    msgs = _history(6) + [HumanMessage(content="new question")]
    assert _mw().before_model({"messages": msgs}, None) is None


def test_empty_history_is_noop():
    assert _mw().before_model({"messages": []}, None) is None
    assert trailing_repeat([]) == (0, "", "")


def test_trailing_repeat_reports_tool_and_snippet():
    n, tool, snippet = trailing_repeat(_history(4))
    assert n == 4 and tool == "run_command" and snippet == "no default repo"


# ── async path mirrors sync ──────────────────────────────────────────────────


async def test_async_before_model_matches_sync():
    mw = _mw()
    out = await mw.abefore_model({"messages": _history(6)}, None)
    assert out is not None and out.get("jump_to") == "end"
