"""Tests for ToolResultPrunerMiddleware (#2782, ADR 0101 D3/D4).

Prune-before-summarize: at a soft pressure threshold, tool results older than
the newest ``keep_messages`` are rewritten to head+tail stubs — one batched
pass, replacement by message id so AIMessage/ToolMessage pairing survives.
"""

from __future__ import annotations

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from graph.middleware.tool_result_pruner import (
    FALLBACK_TRIGGER_TOKENS,
    STUB_HEAD_CHARS,
    STUB_TAIL_CHARS,
    ToolResultPrunerMiddleware,
    _est_tokens,
)


def _mw(**kw):
    defaults = dict(max_input_tokens=100_000, at_fraction=0.6, keep_messages=2, min_chars=4_000)
    defaults.update(kw)
    return ToolResultPrunerMiddleware(**defaults)


def _thread(big_chars=80_000):
    """[human, AI(tool_call), BIG tool result, AI, human] — result is old enough
    to prune with keep_messages=2 (the last two messages are protected)."""
    big = "A" * (big_chars // 2) + "Z" * (big_chars - big_chars // 2)
    return [
        HumanMessage(content="q"),
        AIMessage(content="", tool_calls=[{"name": "fetch_url", "args": {}, "id": "c1", "type": "tool_call"}]),
        ToolMessage(content=big, tool_call_id="c1", name="fetch_url", id="tm-1"),
        AIMessage(content="analyzed"),
        HumanMessage(content="next"),
    ]


def test_prunes_old_oversized_result_over_threshold():
    msgs = _thread(big_chars=400_000)  # ~100k est tokens > 60% of 100k window
    out = _mw().before_model({"messages": msgs}, None)
    assert out is not None
    (pruned,) = out["messages"]
    # Replacement by id — the reducer swaps it in place, pairing intact.
    assert pruned.id == "tm-1" and pruned.tool_call_id == "c1"
    assert pruned.content.startswith("A" * STUB_HEAD_CHARS)
    assert pruned.content.endswith("Z" * STUB_TAIL_CHARS)
    assert "chars pruned by protoAgent" in pruned.content
    assert "re-run the tool" in pruned.content
    # The original stored message was copied, never mutated.
    assert len(msgs[2].content) == 400_000


def test_noop_under_pressure_threshold():
    msgs = _thread(big_chars=100_000)  # ~25k est tokens < 60k threshold
    assert _mw().before_model({"messages": msgs}, None) is None


def test_recent_results_are_protected():
    # Same size, but keep_messages covers the whole thread → nothing eligible.
    msgs = _thread(big_chars=400_000)
    assert _mw(keep_messages=10).before_model({"messages": msgs}, None) is None


def test_small_results_and_already_pruned_are_skipped():
    mw = _mw()
    msgs = _thread(big_chars=400_000)
    first = mw.before_model({"messages": msgs}, None)
    assert first is not None
    # Apply the replacement, then run again at the same pressure: the stub is
    # sentinel-marked and small — no second rewrite, no churn.
    msgs[2] = first["messages"][0]
    assert mw.before_model({"messages": msgs}, None) is None


def test_fallback_threshold_without_a_window_profile():
    # No gateway profile → the fixed conservative floor, mirroring compaction's
    # messages-count degradation: still bounded, never dependent on the gateway.
    mw = _mw(max_input_tokens=None)
    small = _thread(big_chars=100_000)  # ~25k est < 80k fallback
    assert mw.before_model({"messages": small}, None) is None
    big = _thread(big_chars=4 * FALLBACK_TRIGGER_TOKENS + 100_000)
    assert mw.before_model({"messages": big}, None) is not None


def test_batched_pass_prunes_every_eligible_result():
    big = "B" * 200_000
    msgs = [
        HumanMessage(content="q"),
        ToolMessage(content=big, tool_call_id="c1", name="t1", id="tm-1"),
        ToolMessage(content=big, tool_call_id="c2", name="t2", id="tm-2"),
        AIMessage(content="…"),
        HumanMessage(content="next"),
    ]
    out = _mw().before_model({"messages": msgs}, None)
    assert out is not None and len(out["messages"]) == 2  # one pass, both rewritten


def test_est_tokens_counts_block_content():
    msgs = [AIMessage(content=[{"type": "text", "text": "x" * 400}, {"type": "thinking", "thinking": "y" * 400}])]
    assert _est_tokens(msgs) == 200
