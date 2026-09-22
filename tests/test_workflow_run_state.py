"""Tests for workflow run-state persistence (WorkflowRunStore + the _execute audit trail)."""

from __future__ import annotations

import asyncio
import json
import uuid
from types import SimpleNamespace

import plugins.workflows as wf
from plugins.workflows.run_state import STATUS_DONE, STATUS_FAILED, STATUS_RUNNING, STATUS_SEEDED, WorkflowRunStore

RECIPE = {
    "name": "demo",
    "inputs": [{"name": "topic", "required": True}],
    "steps": [
        {"id": "gather", "subagent": "researcher", "prompt": "research {{inputs.topic}}"},
        {
            "id": "brief",
            "subagent": "researcher",
            "depends_on": ["gather"],
            "prompt": "write up:\n{{steps.gather.output}}",
        },
    ],
    "output": "{{steps.brief.output}}",
}


class _FakeReg:
    def __init__(self, recipe=RECIPE):
        self._recipe = recipe

    def get(self, name):
        return self._recipe if name == self._recipe["name"] else None


def _patch_sdk(monkeypatch, run_subagent, workflow_dir=""):
    monkeypatch.setattr(wf.sdk, "subagent_types", lambda: {"researcher"})
    monkeypatch.setattr(wf.sdk, "run_subagent", run_subagent)
    monkeypatch.setattr(
        wf.sdk, "config", lambda: SimpleNamespace(subagent_max_concurrency=2, workflow_dir=workflow_dir)
    )


# --- WorkflowRunStore ---------------------------------------------------------


def test_start_creates_run_file_with_all_fields(tmp_path):
    store = WorkflowRunStore(tmp_path / ".runs")
    run_id = store.start("demo", {"topic": "ai"})
    uuid.UUID(run_id)  # a real UUID, not a slug
    assert store.run_id == run_id
    state = json.loads((tmp_path / ".runs" / f"{run_id}.json").read_text(encoding="utf-8"))
    assert state["run_id"] == run_id
    assert state["recipe_name"] == "demo"
    assert state["inputs"] == {"topic": "ai"}
    assert state["step_outputs"] == {}
    assert state["status"] == STATUS_RUNNING
    assert state["pending_step"] is None
    assert state["created_at"] and state["updated_at"]


def test_step_done_and_finish_update_the_file(tmp_path):
    store = WorkflowRunStore(tmp_path)
    run_id = store.start("demo", {})
    store.step_done("gather", "found things")
    assert store.load(run_id)["step_outputs"] == {"gather": "found things"}
    assert store.load(run_id)["status"] == STATUS_RUNNING
    store.step_done("brief", "wrote it up")
    store.finish(STATUS_DONE)
    state = store.load(run_id)
    assert state["status"] == STATUS_DONE
    assert state["step_outputs"] == {"gather": "found things", "brief": "wrote it up"}


def test_run_state_survives_restart(tmp_path):
    store = WorkflowRunStore(tmp_path)
    run_id = store.start("demo", {"topic": "ai"})
    store.step_done("gather", "out")
    # A fresh store over the same dir (= new process after a server restart)
    # sees the persisted state from disk.
    reopened = WorkflowRunStore(tmp_path)
    state = reopened.load(run_id)
    assert state["recipe_name"] == "demo"
    assert state["step_outputs"] == {"gather": "out"}
    assert state["status"] == STATUS_RUNNING
    assert reopened.list_runs() == [run_id]


def test_load_missing_or_corrupt_returns_none(tmp_path):
    store = WorkflowRunStore(tmp_path)
    assert store.load("nope") is None
    (tmp_path / "bad.json").write_text("{not json", encoding="utf-8")
    assert store.load("bad") is None


# --- _execute wiring ----------------------------------------------------------


def test_execute_persists_a_completed_run(tmp_path, monkeypatch):
    async def run_subagent(subagent_type, prompt, description=""):
        return f"<{description.rsplit(':', 1)[-1]}-out>"

    _patch_sdk(monkeypatch, run_subagent)
    store = WorkflowRunStore(tmp_path)
    result = asyncio.run(wf._execute(_FakeReg(), "demo", {"topic": "ai"}, run_store=store))

    # Existing behavior unchanged — same result flow, plus the run_id.
    assert result["output"] == "<brief-out>"
    assert result["failed"] == []
    assert result["run_id"] == store.run_id

    state = store.load(result["run_id"])
    assert state["status"] == STATUS_DONE
    assert state["step_outputs"] == {"gather": "<gather-out>", "brief": "<brief-out>"}
    assert state["inputs"] == {"topic": "ai"}
    assert state["pending_step"] is None


def test_execute_with_seeded_steps_dispatches_only_what_is_downstream(tmp_path, monkeypatch):
    # #3571: a caller holding a finished run's outputs re-runs ONE late step. `gather` is
    # handed in, so only `brief` is dispatched — and it sees the seeded text, not a rerun.
    dispatched = []

    async def run_subagent(subagent_type, prompt, description=""):
        dispatched.append((description.rsplit(":", 1)[-1], prompt))
        return "brief-from-seed"

    _patch_sdk(monkeypatch, run_subagent)
    store = WorkflowRunStore(tmp_path)
    result = asyncio.run(
        wf._execute(_FakeReg(), "demo", {"topic": "ai"}, run_store=store, seed_outputs={"gather": "handed in"})
    )
    assert [d[0] for d in dispatched] == ["brief"]
    assert dispatched[0][1] == "write up:\nhanded in"
    assert result["output"] == "brief-from-seed" and result["failed"] == []
    state = store.load(result["run_id"])
    # The record says what happened: the seeded step is `seeded`, never `done` or `running`.
    assert state["step_meta"]["gather"] == {"status": STATUS_SEEDED}
    assert state["step_outputs"] == {"gather": "handed in", "brief": "brief-from-seed"}
    assert state["step_meta"]["brief"]["status"] == STATUS_DONE


def test_the_record_says_what_each_step_was_handed(tmp_path, monkeypatch):
    # A subagent that claims its input "is absent from the message" can be checked
    # against data: the rendered prompt's length and digest are on the record.
    import hashlib

    async def run_subagent(subagent_type, prompt, description=""):
        return "x"

    _patch_sdk(monkeypatch, run_subagent)
    store = WorkflowRunStore(tmp_path)
    result = asyncio.run(wf._execute(_FakeReg(), "demo", {"topic": "ai"}, run_store=store))
    meta = store.load(result["run_id"])["step_meta"]["brief"]
    rendered = "write up:\nx"
    assert meta["prompt_chars"] == len(rendered)
    assert meta["prompt_sha256"] == hashlib.sha256(rendered.encode()).hexdigest()


def test_state_workflow_run_takes_seed_outputs(monkeypatch, tmp_path):
    # The public runner a plugin gets (STATE.workflow_run) carries the keyword through.
    from runtime.state import STATE

    seen = {}

    async def fake_execute(reg, name, inputs, on_step=None, seed_outputs=None):
        seen.update(name=name, seed=seed_outputs)
        return {"output": "", "steps": {}, "failed": []}

    class _Registry:
        def __init__(self):
            from fastapi import FastAPI

            self.app = FastAPI()
            self.config = {}

        def register_router(self, router, prefix):
            pass

        def register_tools(self, tools):
            pass

        def register_workflow_dir(self, d):
            pass

        def emit(self, topic, data):
            pass

    monkeypatch.setattr(wf, "_execute", fake_execute)
    _patch_sdk(monkeypatch, None, workflow_dir=str(tmp_path / "wfdir"))
    prev = (getattr(STATE, "workflow_registry", None), getattr(STATE, "workflow_run", None))
    try:
        wf.register(_Registry())
        asyncio.run(STATE.workflow_run("demo", {"topic": "ai"}, seed_outputs={"gather": "g"}))
    finally:
        STATE.workflow_registry, STATE.workflow_run = prev
    assert seen == {"name": "demo", "seed": {"gather": "g"}}


def test_execute_persists_a_failed_run(tmp_path, monkeypatch):
    async def run_subagent(subagent_type, prompt, description=""):
        if description.endswith(":gather"):
            raise RuntimeError("boom")
        return "brief-out"

    _patch_sdk(monkeypatch, run_subagent)
    store = WorkflowRunStore(tmp_path)
    result = asyncio.run(wf._execute(_FakeReg(), "demo", {"topic": "ai"}, run_store=store))

    # The run still completed end-to-end (engine semantics: errors recorded inline).
    assert result["failed"] == ["gather"]
    state = store.load(result["run_id"])
    assert state["status"] == STATUS_FAILED
    # Completed steps AND the failed step's error text are both on the record.
    assert state["step_outputs"]["brief"] == "brief-out"
    assert "Error: step 'gather'" in state["step_outputs"]["gather"]
    assert "boom" in state["step_outputs"]["gather"]


def test_execute_default_store_lands_under_writable_dir(tmp_path, monkeypatch):
    async def run_subagent(subagent_type, prompt, description=""):
        return "out"

    _patch_sdk(monkeypatch, run_subagent, workflow_dir=str(tmp_path / "wfdir"))
    result = asyncio.run(wf._execute(_FakeReg(), "demo", {"topic": "ai"}))
    run_file = tmp_path / "wfdir" / ".runs" / f"{result['run_id']}.json"
    assert run_file.exists()
    assert json.loads(run_file.read_text(encoding="utf-8"))["status"] == STATUS_DONE


def test_validation_failure_creates_no_run_file(tmp_path, monkeypatch):
    async def run_subagent(subagent_type, prompt, description=""):
        raise AssertionError("should never run")

    _patch_sdk(monkeypatch, run_subagent)
    store = WorkflowRunStore(tmp_path)
    try:
        asyncio.run(wf._execute(_FakeReg(), "demo", {}, run_store=store))  # missing required input
        raise AssertionError("expected ValueError")
    except ValueError:
        pass
    assert store.list_runs() == []
    assert store.run_id is None


def test_writable_dir_expands_tilde(monkeypatch):
    """A `~` in workflow_dir must expand (pre-refactor behavior) — never a literal
    `~` directory (QA panel finding on the slice-1 PR)."""
    from types import SimpleNamespace

    from plugins import workflows as wf

    monkeypatch.setattr(wf.sdk, "config", lambda: SimpleNamespace(workflow_dir="~/wf-store"))
    out = wf._writable_dir()
    assert "~" not in str(out)
    assert out.is_absolute()  # expanded to a real absolute dir (drive-anchored on Windows)
