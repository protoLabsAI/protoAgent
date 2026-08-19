"""Context audit: tool args counted ONCE, categories honest, walker contract holds.

The motivating bug: an Anthropic-style AIMessage's ``content`` block list already
contains the ``tool_use`` blocks, and ``tool_calls`` mirrors them — a walker that
sums both overstates a thread (a live audit read ~34k phantom tokens). These tests
pin the count-once contract at both layers: ``graph.message_blocks`` (text_of never
leaks tool args) and ``graph.context_audit_op`` (the breakdown counts args once).
"""

from __future__ import annotations

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from graph.context_audit_op import audit_messages
from graph.message_blocks import role_of, text_of, tool_calls_of

BIG_ARGS = {"path": "tools.py", "content": "x" * 4000}


def _ai_with_tool_use() -> AIMessage:
    """The Anthropic shape: content carries text AND the tool_use block (args included);
    tool_calls mirrors the same call."""
    return AIMessage(
        content=[
            {"type": "text", "text": "Writing the file now."},
            {"type": "tool_use", "id": "tu_1", "name": "plugin_write_file", "input": BIG_ARGS},
        ],
        tool_calls=[{"id": "tu_1", "name": "plugin_write_file", "args": BIG_ARGS, "type": "tool_call"}],
    )


# ── message_blocks: the walker contract ────────────────────────────────────────


def test_text_of_never_leaks_tool_args():
    text = text_of(_ai_with_tool_use())
    assert "Writing the file now." in text
    assert "x" * 100 not in text  # the 4k-char payload must not ride the text channel
    assert "_[tool_use]_" in text  # placeholder, not payload


def test_tool_calls_of_returns_args_exactly_once():
    calls = tool_calls_of(_ai_with_tool_use())
    assert len(calls) == 1 and calls[0]["args"] == BIG_ARGS


def test_walker_tolerates_plain_dicts():
    d = {"role": "assistant", "content": "hi", "tool_calls": [{"name": "t", "args": {}}]}
    assert role_of(d) == "assistant" and text_of(d) == "hi" and len(tool_calls_of(d)) == 1


# ── context_audit_op: the breakdown ────────────────────────────────────────────


def test_tool_args_counted_once():
    """THE regression: content + tool_calls carry the same 4k payload; the audit
    must attribute it to tool_call_args once and keep assistant_text tiny."""
    report = audit_messages([_ai_with_tool_use()])
    args_tok = report["tool_call_args"]["plugin_write_file"]
    assert args_tok >= 900  # ~4k chars / 4
    assert report["categories"]["assistant_text"] < 20  # the sentence + placeholder only
    assert report["total_est_tokens"] < args_tok * 1.2  # nothing counted twice


def test_categories_split_frames_operators_and_results():
    msgs = [
        HumanMessage(content="<injected_context>\n<injected_memory>recall…</injected_memory>"),
        HumanMessage(content="build me a reddit plugin"),
        _ai_with_tool_use(),
        ToolMessage(content="✓ wrote tools.py", tool_call_id="tu_1", name="plugin_write_file"),
    ]
    report = audit_messages(msgs)
    cats = report["categories"]
    assert cats["injected_context_frames"] > 0
    assert cats["operator_messages"] > 0
    assert report["tool_results"]["plugin_write_file"]["calls"] == 1
    assert report["message_count"] == 4


def test_top_blocks_ranked_and_capped():
    msgs = [_ai_with_tool_use() for _ in range(5)]
    report = audit_messages(msgs, top_n=3)
    tops = report["top_blocks"]
    assert len(tops) == 3
    assert tops[0]["est_tokens"] >= tops[-1]["est_tokens"]
    assert all(len(b["preview"]) <= 80 for b in tops)
