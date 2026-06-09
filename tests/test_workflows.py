"""Tests for the declarative workflow engine + registry (ADR 0002)."""

from __future__ import annotations

import asyncio

from plugins.workflows.engine import (
    execute_workflow,
    render_template,
    resolve_inputs,
    validate_recipe,
)
from plugins.workflows.registry import WorkflowRegistry

VALID = {
    "name": "demo",
    "inputs": [{"name": "topic", "required": True}, {"name": "depth", "default": "deep"}],
    "steps": [
        {"id": "gather", "subagent": "researcher", "prompt": "research {{inputs.topic}} ({{inputs.depth}})"},
        {"id": "brief", "subagent": "researcher", "depends_on": ["gather"],
         "prompt": "write up:\n{{steps.gather.output}}"},
    ],
    "output": "{{steps.brief.output}}",
}


def test_validate_accepts_valid_recipe():
    assert validate_recipe(VALID, known_subagents={"researcher"}) == []


def test_validate_catches_structural_errors():
    assert "missing 'name'" in validate_recipe({"steps": [{"id": "a", "subagent": "researcher", "prompt": "x"}]})
    assert any("non-empty list" in e for e in validate_recipe({"name": "x"}))
    dup = {"name": "x", "steps": [
        {"id": "a", "subagent": "researcher", "prompt": "p"},
        {"id": "a", "subagent": "researcher", "prompt": "p"},
    ]}
    assert any("duplicate step id" in e for e in validate_recipe(dup))


def test_validate_catches_dep_and_cycle_and_subagent():
    bad_dep = {"name": "x", "steps": [{"id": "a", "subagent": "researcher", "prompt": "p", "depends_on": ["z"]}]}
    assert any("unknown step 'z'" in e for e in validate_recipe(bad_dep))
    cycle = {"name": "x", "steps": [
        {"id": "a", "subagent": "researcher", "prompt": "p", "depends_on": ["b"]},
        {"id": "b", "subagent": "researcher", "prompt": "p", "depends_on": ["a"]},
    ]}
    assert any("cycle" in e for e in validate_recipe(cycle))
    unknown_sub = {"name": "x", "steps": [{"id": "a", "subagent": "nope", "prompt": "p"}]}
    assert any("unknown subagent" in e for e in validate_recipe(unknown_sub, known_subagents={"researcher"}))


def test_validate_catches_bad_template_refs():
    bad = {"name": "x", "inputs": [{"name": "topic"}], "steps": [
        {"id": "a", "subagent": "researcher", "prompt": "{{inputs.missing}} {{steps.ghost.output}}"},
    ]}
    errs = validate_recipe(bad)
    assert any("unknown input" in e for e in errs)
    assert any("unknown step" in e for e in errs)


def test_render_template_substitutes():
    out = render_template("hi {{inputs.topic}} / {{steps.s.output}}", {"topic": "x"}, {"s": "RESULT"})
    assert out == "hi x / RESULT"


def test_resolve_inputs_defaults_and_missing():
    resolved, missing = resolve_inputs(VALID, {"topic": "ai"})
    assert resolved["topic"] == "ai" and resolved["depth"] == "deep" and missing == []
    _, missing2 = resolve_inputs(VALID, {})
    assert missing2 == ["topic"]


def test_execute_threads_outputs_sequentially():
    calls = []

    async def run_step(subagent, prompt, sid):
        calls.append((sid, prompt))
        return f"<{sid}-out>"

    res = asyncio.run(execute_workflow(VALID, {"topic": "ai", "depth": "deep"}, run_step=run_step))
    # gather ran first; brief's prompt saw gather's output threaded in.
    brief_prompt = dict((sid, p) for sid, p in calls)["brief"]
    assert "<gather-out>" in brief_prompt
    assert res["output"] == "<brief-out>"
    assert res["failed"] == []


def test_execute_runs_independent_steps_in_parallel():
    running = 0
    max_seen = 0

    async def run_step(subagent, prompt, sid):
        nonlocal running, max_seen
        running += 1
        max_seen = max(max_seen, running)
        await asyncio.sleep(0.02)
        running -= 1
        return sid

    fanout = {"name": "f", "steps": [
        {"id": "a", "subagent": "researcher", "prompt": "p"},
        {"id": "b", "subagent": "researcher", "prompt": "p"},
        {"id": "c", "subagent": "researcher", "prompt": "p"},
    ]}
    asyncio.run(execute_workflow(fanout, {}, run_step=run_step, max_concurrency=4))
    assert max_seen >= 2  # independent steps overlapped


def test_execute_records_failure_inline_and_continues():
    async def run_step(subagent, prompt, sid):
        if sid == "gather":
            raise RuntimeError("boom")
        return f"saw:{prompt}"

    res = asyncio.run(execute_workflow(VALID, {"topic": "ai"}, run_step=run_step))
    assert "gather" in res["failed"]
    # brief still ran and saw the error text from gather.
    assert "Error: step 'gather'" in res["steps"]["brief"]


def test_registry_save_roundtrip_and_override(tmp_path):
    bundled = tmp_path / "bundled"
    writable = tmp_path / "writable"
    bundled.mkdir()
    (bundled / "demo.yaml").write_text(
        "name: demo\ndescription: bundled\nsteps:\n  - id: a\n    subagent: researcher\n    prompt: p\n",
        encoding="utf-8",
    )
    reg = WorkflowRegistry([str(bundled), str(writable)], writable_dir=str(writable))
    assert reg.get("demo")["description"] == "bundled"
    # Save overrides (writable dir wins) + is immediately runnable.
    reg.save({"name": "demo", "description": "saved", "steps": [{"id": "a", "subagent": "researcher", "prompt": "p"}]})
    assert reg.get("demo")["description"] == "saved"
    assert (writable / "demo.yaml").exists()
    # New recipe persists + loads.
    reg.save({"name": "Fresh One", "description": "x", "steps": [{"id": "a", "subagent": "researcher", "prompt": "p"}]})
    assert "Fresh One" in reg.names()
    assert (writable / "fresh-one.yaml").exists()  # slugified filename


def test_registry_delete(tmp_path):
    reg = WorkflowRegistry([str(tmp_path)], writable_dir=str(tmp_path))
    reg.save({"name": "temp", "description": "x", "steps": [{"id": "a", "subagent": "researcher", "prompt": "p"}]})
    assert "temp" in reg.names()
    assert reg.delete("temp") is True
    assert "temp" not in reg.names()
    assert reg.delete("temp") is False


def test_registry_loads_and_lists(tmp_path):
    (tmp_path / "w.yaml").write_text(
        "name: wf\ndescription: d\nsteps:\n  - id: a\n    subagent: researcher\n    prompt: p\n",
        encoding="utf-8",
    )
    (tmp_path / "bad.yaml").write_text("just a string", encoding="utf-8")  # ignored
    reg = WorkflowRegistry([str(tmp_path)])
    assert reg.names() == ["wf"]
    assert reg.get("wf")["description"] == "d"
    assert reg.list()[0]["name"] == "wf"
