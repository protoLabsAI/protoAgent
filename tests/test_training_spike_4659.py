"""Offline oracle checks for the held-out routing probes, not model baselines."""

import asyncio
import json
from pathlib import Path

import pytest

from evals import runner, verify
from evals.client import TaskResult

CASES = [
    case
    for case in json.loads((Path(__file__).parents[1] / "evals/tasks.json").read_text())
    if case["category"] == "routing-spike-4659"
]


@pytest.fixture
def oracle(monkeypatch):
    """Fake only external evidence; exercise the runner's actual assertions."""
    evidence = {"audit": [], "chunk": {"id": 1}, "teardown": []}
    monkeypatch.setattr(verify, "audit_entries_since", lambda _: evidence["audit"])
    monkeypatch.setattr(verify, "find_chunk_containing", lambda *a, **kw: evidence["chunk"])
    monkeypatch.setattr(verify, "apply_teardown", lambda steps: evidence["teardown"].extend(steps))
    monkeypatch.setattr(runner, "_AUDIT_POLL_DEADLINE_S", 0.0)
    return evidence


def run_case(case):
    class Client:
        async def ask(self, prompt, **kwargs):
            return TaskResult("synthetic", "completed", text=" ".join(case.get("expected_patterns", [])))

    return asyncio.run(runner._run_prompt_case(Client(), case, streaming=False))


@pytest.mark.parametrize("case", CASES, ids=lambda c: c["id"])
def test_routing_oracle_accepts_valid_evidence_and_rejects_wrong_domain(case, oracle):
    oracle["audit"] = [{"tool": tool, "success": True} for tool in case["expected_tools"]]
    assert run_case(case).passed
    # Fluent output alone must not hide a wrong-domain call, including abstention.
    oracle["audit"] = [{"tool": "wrong_domain_tool", "success": True}]
    assert not run_case(case).passed


@pytest.mark.parametrize("case", [c for c in CASES if c["expected_tools"]], ids=lambda c: c["id"])
def test_routing_oracle_rejects_failed_tool_calls(case, oracle):
    oracle["audit"] = [{"tool": tool, "success": False} for tool in case["expected_tools"]]
    assert not run_case(case).passed


@pytest.mark.parametrize("case", [c for c in CASES if c.get("forbidden_tools")], ids=lambda c: c["id"])
def test_routing_oracle_rejects_extra_forbidden_calls_even_when_they_error(case, oracle):
    oracle["audit"] = [{"tool": tool, "success": True} for tool in case["expected_tools"]]
    oracle["audit"] += [{"tool": tool, "success": False} for tool in case["forbidden_tools"]]
    assert not run_case(case).passed


def test_save_probe_requires_persistence_and_cleans_up_on_failure(oracle):
    case = next(c for c in CASES if c["id"] == "route4659_save")
    oracle["audit"] = [{"tool": "memory_ingest", "success": True}]
    oracle["chunk"] = None
    result = run_case(case)
    assert not result.passed
    assert "no chunk containing" in result.detail
    assert oracle["teardown"] == case["teardown"]
