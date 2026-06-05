"""Tests for workflow slash-command parsing (ADR 0002 — /<workflow> in chat)."""

from __future__ import annotations

import server

RECIPE = {
    "name": "research-and-brief",
    "inputs": [{"name": "topic", "required": True}, {"name": "depth", "default": "deep"}],
}


def test_parse_slash_command_splits_name_and_rest():
    assert server._parse_slash_command("/research-and-brief quantum computing") == (
        "research-and-brief",
        "quantum computing",
    )
    assert server._parse_slash_command("/bare") == ("bare", "")
    assert server._parse_slash_command("not a command") == ("", "")
    assert server._parse_slash_command("   ") == ("", "")


def test_free_text_maps_to_first_required_input():
    assert server._parse_workflow_inputs(RECIPE, "quantum computing") == {"topic": "quantum computing"}


def test_key_value_tokens_set_named_inputs_with_quotes():
    out = server._parse_workflow_inputs(RECIPE, 'topic="quantum error correction" depth=shallow')
    assert out == {"topic": "quantum error correction", "depth": "shallow"}


def test_mixed_free_text_and_key_value():
    # key=value sets depth; the leftover free text fills the first unset required input
    out = server._parse_workflow_inputs(RECIPE, "quantum computing depth=shallow")
    assert out == {"depth": "shallow", "topic": "quantum computing"}


def test_no_inputs_recipe_ignores_free_text():
    out = server._parse_workflow_inputs({"inputs": []}, "whatever text")
    assert out == {}


def test_parse_workflow_command_returns_none_without_registry():
    # _workflow_registry is None in a bare import (no graph built) → not a wf command
    assert server.STATE.workflow_registry is None
    assert server._parse_workflow_command("/research-and-brief topic=x") is None
    assert server._parse_workflow_command("hello") is None
