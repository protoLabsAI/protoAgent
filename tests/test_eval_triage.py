"""Proposal-only nightly triage: real tools/agent loop, fake model and I/O boundaries."""
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from langchain.agents import create_agent
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain_core.tools import tool

from plugins import friction


@pytest.fixture
def regression(tmp_path, monkeypatch):
    path = tmp_path / "friction.jsonl"
    monkeypatch.setenv("FRICTION_LOG", str(path))
    row = {"kind": "harness", "summary": "nightly gate regression: instance:lookup",
           "severity": "major", "source": "plugin:evals-nightly", "trace_id": "trace-1",
           "detail": "report: run.json\ntrace: https://trace.example/trace-1"}
    path.write_text(json.dumps(row) + "\n", encoding="utf-8")
    return row, path


@pytest.mark.asyncio
async def test_reads_only_open_nightly_entries_without_latest_twelve_cap(regression):
    row, path = regression
    rows = [row, {**row, "source": "agent"}, {**row, "resolved_at": "yesterday"},
            {**row, "summary": "unrelated"}]
    rows += [{**row, "summary": f"nightly gate regression: case-{i}"} for i in range(15)]
    path.write_text("\n".join(map(json.dumps, rows)), encoding="utf-8")
    result = json.loads(await friction.eval_regression_entries.ainvoke({}))
    assert len(result) == 16
    assert result[0]["case_id"] == "instance:lookup"
    assert result[0]["id"] == friction.entry_id("harness", row["summary"])


class Model(GenericFakeChatModel):
    def bind_tools(self, tools, **kwargs):
        assert {t.name for t in tools} == friction.eval_triage_tool_names()
        return self


@pytest.mark.asyncio
async def test_repeated_nights_share_one_proposal_identity_and_latest_evidence(regression):
    row, path = regression
    latest = {**row, "trace_id": "trace-2", "detail": "trace: https://trace.example/trace-2"}
    unrelated = {**row, "source": "agent", "trace_id": "not-nightly"}
    path.write_text("\n".join(map(json.dumps, [row, latest, unrelated])), encoding="utf-8")
    entries = json.loads(await friction.eval_regression_entries.ainvoke({}))
    assert len(entries) == 1
    assert entries[0]["trace_id"] == "trace-2"
    assert entries[0]["detail"] == latest["detail"]
    assert entries[0]["id"] == friction.entry_id("harness", row["summary"])


def call(name, **args):
    return AIMessage(content="", tool_calls=[{"name": name, "args": args, "id": name}])


@pytest.mark.asyncio
@pytest.mark.parametrize("classification", ["harness", "case", "flake"])
async def test_seeded_regression_proposal_through_agent(regression, monkeypatch, classification):
    row, path = regression
    original = path.read_bytes()
    rid = friction.entry_id("harness", row["summary"])
    trace = Mock(return_value={"trace_url": "https://trace.example/trace-1", "trace": "lookup never called"})
    runner = Mock(return_value={"id": "instance:lookup", "passed": classification == "flake",
                               "skipped": False, "detail": "missing expected lookup"})
    monkeypatch.setattr(friction, "_read_regression_trace", trace)
    monkeypatch.setattr(friction, "_rerun_eval_case", runner)
    proposals = []

    @tool
    def task_create(title: str, description: str) -> str:
        """Fake proposal sink."""
        proposals.append({"title": title, "description": description})
        return "Created proposal-1"

    evidence = f"instance:lookup https://trace.example/trace-1 friction {rid}"
    steps = [call("eval_regression_entries"), call("eval_regression_trace", trace_id="trace-1"),
             call("eval_rerun_case", case_id="instance:lookup")]
    if classification == "harness":
        steps.append(call("task_create", title="Investigate lookup regression", description=(
            f"{evidence}; assertion: missing expected lookup; suspected cause: routing defect")))
        answer = "Proposal proposal-1: " + evidence
    elif classification == "case":
        answer = f"Case-edit proposal: {evidence}\nCorrect stale fixture data.\n{friction.EVAL_CASE_EDIT_GUARDRAIL}"
    else:
        answer = f"Flake: {evidence}; demote to tracked or fix"
    steps.append(AIMessage(content=answer))
    spec = friction._build_eval_regression_subagent()
    agent = create_agent(Model(messages=iter(steps)), system_prompt=spec.system_prompt,
                         tools=[friction.eval_regression_entries, friction.eval_regression_trace,
                                friction.eval_rerun_case, task_create])
    result = await agent.ainvoke({"messages": [HumanMessage(content=spec.default_prompt)]})
    final = result["messages"][-1].content
    assert evidence in final
    trace.assert_called_once_with("trace-1")
    runner.assert_called_once_with("instance:lookup")
    assert len(proposals) == (1 if classification == "harness" else 0)
    if classification == "case":
        assert friction.EVAL_CASE_EDIT_GUARDRAIL in final
        assert friction.EVAL_CASE_EDIT_GUARDRAIL in spec.system_prompt
    if classification == "flake":
        assert "demote to tracked or fix" in final
    outputs = [m for m in result["messages"] if isinstance(m, ToolMessage)]
    assert all(m.status != "error" for m in outputs)
    assert path.read_bytes() == original


def test_registered_allowlist_and_existing_reviewer(monkeypatch):
    from graph.plugins.testkit import FakeRegistry

    registry = FakeRegistry(plugin_id="friction", plugin_dir=Path(friction.__file__).parent)
    monkeypatch.setattr(friction, "_REGISTRY", None)
    friction.register(registry)
    spec = next(s for s in registry.subagents if s.name == "eval_regression_triage")
    assert set(spec.tools) == friction.eval_triage_tool_names() == {
        "eval_regression_entries", "eval_regression_trace", "eval_rerun_case", "task_create"}
    assert not spec.inherit_read_tools
    assert not spec.allow_skill_emission
    assert not set(spec.tools) & {"resolve_friction", "write_file", "edit_file", "run_command", "task"}
    old = next(s for s in registry.subagents if s.name == "friction_triage")
    assert old.tools == ["friction_review"]
    assert not old.inherit_read_tools


@pytest.mark.asyncio
@pytest.mark.parametrize("case_id", ["", "--help", "one,two", "one --out evals/tasks.json", "one\ntwo"])
async def test_runner_rejects_multiple_cases_and_options(monkeypatch, case_id):
    runner = Mock()
    monkeypatch.setattr(friction, "_rerun_eval_case", runner)
    assert "error" in json.loads(await friction.eval_rerun_case.ainvoke({"case_id": case_id}))
    runner.assert_not_called()


@pytest.mark.platform_sensitive
@pytest.mark.parametrize("rows", [[], [{"id": "wrong", "passed": True}],
                                  [{"id": "instance:lookup"}, {"id": "extra"}]])
def test_runner_rejects_empty_or_wrong_report(monkeypatch, rows):
    def run(argv, **kwargs):
        Path(argv[-1]).write_text(json.dumps({"results": rows}), encoding="utf-8")
        return SimpleNamespace(returncode=0)
    monkeypatch.setattr("subprocess.run", run)
    with pytest.raises(ValueError, match="exactly"):
        friction._rerun_eval_case("instance:lookup")


@pytest.mark.platform_sensitive
def test_runner_fixed_command_and_skipped_result(monkeypatch):
    row = {"id": "instance:lookup", "passed": True, "skipped": True}
    def run(argv, **kwargs):
        assert argv[1:6] == ["-m", "evals.runner", "--tasks", "instance:lookup", "--out"]
        assert kwargs.get("shell", False) is False
        assert kwargs["timeout"] == 600
        assert Path(argv[-1]).name == "report.json"
        Path(argv[-1]).write_text(json.dumps({"results": [row]}), encoding="utf-8")
        return SimpleNamespace(returncode=0)
    monkeypatch.setattr("subprocess.run", run)
    assert friction._rerun_eval_case("instance:lookup") == row
    assert "skipped is false" in friction._build_eval_regression_subagent().system_prompt


@pytest.mark.asyncio
async def test_unavailable_evidence_is_explicit(regression, monkeypatch):
    monkeypatch.setattr(friction, "_read_regression_trace", Mock(side_effect=TimeoutError))
    monkeypatch.setattr(friction, "_rerun_eval_case", Mock(side_effect=ValueError))
    assert json.loads(await friction.eval_regression_trace.ainvoke({"trace_id": "trace-1"}))["unavailable"]
    assert json.loads(await friction.eval_rerun_case.ainvoke({"case_id": "lookup"}))["unavailable"]
    assert "error" in json.loads(await friction.eval_regression_trace.ainvoke({"trace_id": "unrelated"}))


def test_trace_backend_read_is_bounded_and_does_not_score(monkeypatch):
    client = Mock()
    client.api.trace.get.return_value.dict.return_value = {"input": "find the record"}
    client.get_trace_url.return_value = "https://trace.example/trace-1"
    monkeypatch.setattr(friction.tracing, "_langfuse", client)
    monkeypatch.setattr(friction.tracing, "_enabled", True)
    result = friction._read_regression_trace("trace-1")
    assert result["trace"] == {"input": "find the record"}
    assert result["trace_url"] == "https://trace.example/trace-1"
    client.api.trace.get.assert_called_once_with(
        "trace-1", request_options={"timeout_in_seconds": 20, "max_retries": 0})
    client.create_score.assert_not_called()
