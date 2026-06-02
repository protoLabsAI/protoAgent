"""Server-side HITL form primitive (Sprint A): the `request_user_input` tool +
the interrupt→input-required payload shaping that lets a JSON-schema form survive
to the A2A layer / console (generalizing `ask_human`)."""

from __future__ import annotations

import server
from tools.lg_tools import DEFERRED_BASE_TOOL_NAMES, get_all_tools


def _tool_names() -> set[str]:
    return {getattr(t, "name", "") for t in get_all_tools()}


def test_request_user_input_registered_as_default_lead_tool():
    names = _tool_names()
    assert "request_user_input" in names
    assert "ask_human" in names  # the free-text sibling stays too


def test_request_user_input_in_deferred_base():
    # Must stay visible when tool-deferral is on (it's a core HITL capability).
    assert "request_user_input" in DEFERRED_BASE_TOOL_NAMES


# ── interrupt → input-required payload shaping ────────────────────────────────


def test_form_payload_passes_through():
    val = {"kind": "form", "title": "Pick", "description": "", "steps": [{"schema": {}}]}
    assert server._interrupt_payload(val) is val


def test_question_payload_passes_through():
    assert server._interrupt_payload({"question": "Merge it?"}) == {"question": "Merge it?"}


def test_plain_value_degrades_to_question():
    assert server._interrupt_payload("just text") == {"question": "just text"}
    assert server._interrupt_payload(None) == {"question": "Input required."}
    # A dict that's neither a question nor a form is stringified into a question
    # (never silently dropped).
    out = server._interrupt_payload({"foo": 1})
    assert "question" in out and "foo" in out["question"]
