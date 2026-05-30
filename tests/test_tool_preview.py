"""Tests for the tool-call preview coercion in ``server.py``.

`_run_turn_stream` renders each tool's input/output for the console's
tool-call cards. Structured values must become real JSON (double-quoted,
parseable) — not a Python ``repr`` — and tool outputs must be unwrapped from
the LangChain ``ToolMessage`` to their actual ``.content`` (the message repr
leaks ``name=``/``tool_call_id=`` noise into the card).
"""

from __future__ import annotations

import json

from langchain_core.messages import ToolMessage

from server import _TOOL_PREVIEW_CHARS, _coerce_tool_output, _coerce_tool_value


def test_dict_input_becomes_parseable_json():
    """A dict tool input renders as JSON the console can `JSON.parse` — the
    bug was `str(dict)` emitting single-quoted Python repr no parser accepts."""
    out = _coerce_tool_value({"max_results": 8, "query": "AI coding agents"})
    assert json.loads(out) == {"max_results": 8, "query": "AI coding agents"}
    assert "'" not in out  # double-quoted JSON, not a Python repr


def test_list_input_becomes_json():
    out = _coerce_tool_value(["a", "b"])
    assert json.loads(out) == ["a", "b"]


def test_plain_string_input_passes_through():
    assert _coerce_tool_value("just text") == "just text"


def test_empty_and_none_render_empty():
    assert _coerce_tool_value("") == ""
    assert _coerce_tool_value(None) == ""


def test_non_serializable_falls_back_to_str():
    """A value json.dumps can't handle (even with default=str shouldn't raise)
    still yields a string, never an exception that would drop the frame."""

    class Weird:
        def __repr__(self):
            return "<weird>"

    out = _coerce_tool_value({"obj": Weird()})
    # default=str keeps it serializable
    assert json.loads(out) == {"obj": "<weird>"}


def test_output_unwraps_toolmessage_to_content():
    """The card wants the result, not `content='..' name='..' tool_call_id='..'`."""
    msg = ToolMessage(content="1234 * 5678 = 7006652", name="calculator", tool_call_id="x")
    assert _coerce_tool_output(msg) == "1234 * 5678 = 7006652"


def test_output_dict_content_is_passed_through_as_langchain_stringified_it():
    """LangChain coerces non-string ToolMessage content to a Python-repr
    string at construction (upstream of us), so a dict result arrives as a
    string. We pass it through verbatim — the client's pretty-printer falls
    back to raw text when it isn't valid JSON. Documents the boundary so a
    future reader doesn't expect us to reconstruct the dict."""
    msg = ToolMessage(content={"ok": True}, name="t", tool_call_id="x")
    assert _coerce_tool_output(msg) == "{'ok': True}"


def test_output_plain_value_passes_through():
    assert _coerce_tool_output("plain result") == "plain result"


def test_previews_are_truncated():
    big = {"query": "x" * 5000}
    assert len(_coerce_tool_value(big)) <= _TOOL_PREVIEW_CHARS
    assert len(_coerce_tool_output("y" * 5000)) <= _TOOL_PREVIEW_CHARS
