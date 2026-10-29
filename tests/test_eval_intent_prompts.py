"""Intent probes must leave tool selection to the agent (eval guide prompt rule)."""

import json
from pathlib import Path

import pytest


TASKS = json.loads((Path(__file__).parent.parent / "evals" / "tasks.json").read_text())
INTENT_CASES = [case for case in TASKS if case["id"].endswith("_intent")]
TOOL_NAMES = {tool for case in TASKS for tool in case.get("expected_tools", [])}


@pytest.mark.parametrize("case", INTENT_CASES, ids=lambda case: case["id"])
def test_intent_prompts_do_not_name_tools(case):
    prompt = case["prompt"].casefold()
    for tool in TOOL_NAMES:
        assert tool.casefold() not in prompt, f"{case['id']} gives away tool {tool}"


@pytest.mark.parametrize("case_id,tool", [
    ("calculator_intent", "calculator"),
    ("memory_ingest_intent", "memory_ingest"),
    ("memory_note_intent", "memory_ingest"),
    ("memory_recall_intent", "memory_recall"),
    ("memory_list_intent", "memory_list"),
])
def test_reviewed_intent_cases_keep_tool_assertions(case_id, tool):
    case = next(case for case in TASKS if case["id"] == case_id)
    assert case["expected_tools"] == [tool]
    assert not case.get("requires_env"), "Intent probes must remain in the default suite"
