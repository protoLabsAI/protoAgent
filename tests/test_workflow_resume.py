"""Tests for the workflow resume path (F3) — the operator approve/edit/reject of a
paused `gate: human` run, plus the Pending Gates listing the console renders.

Covers the engine resume knobs (`seed_outputs` / `prompt_overrides` / `prefailed` /
`skip_gate`), the `_resume` orchestration + run-state updates, the rendered-prompt
listing, and resumability after a server restart (state read fresh off disk).
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import plugins.workflows as wf
from plugins.workflows.engine import execute_workflow
from plugins.workflows.run_state import STATUS_DONE, STATUS_FAILED, STATUS_PAUSED, WorkflowRunStore

# gather → analyze (gate: human) → write. A dependent step AFTER the gate so we can
# assert the DAG continues and downstream steps see the resumed step's output.
GATED = {
    "name": "gated",
    "inputs": [{"name": "topic", "required": True}],
    "steps": [
        {"id": "gather", "subagent": "researcher", "prompt": "research {{inputs.topic}}"},
        {
            "id": "analyze",
            "subagent": "researcher",
            "depends_on": ["gather"],
            "prompt": "analyze:\n{{steps.gather.output}}",
            "gate": "human",
        },
        {
            "id": "write",
            "subagent": "researcher",
            "depends_on": ["analyze"],
            "prompt": "write up:\n{{steps.analyze.output}}",
        },
    ],
    "output": "{{steps.write.output}}",
}


class _GatedReg:
    def get(self, name):
        return GATED if name == GATED["name"] else None


def _echo_runner(prompts):
    """A subagent runner that records the prompt each step received and echoes it back
    as the step's output — so an edited prompt is visible in the downstream step's input
    (and the final output)."""

    async def run_subagent(subagent_type, prompt, description=""):
        sid = description.rsplit(":", 1)[-1]
        prompts[sid] = prompt
        return prompt

    return run_subagent


def _patch_sdk(monkeypatch, run_subagent, workflow_dir=""):
    monkeypatch.setattr(wf.sdk, "subagent_types", lambda: {"researcher"})
    monkeypatch.setattr(wf.sdk, "run_subagent", run_subagent)
    monkeypatch.setattr(
        wf.sdk, "config", lambda: SimpleNamespace(subagent_max_concurrency=2, workflow_dir=workflow_dir)
    )


def _pause_gated(monkeypatch, tmp_path, prompts=None):
    """Run the GATED recipe until it parks at `analyze`; return (store, run_id)."""
    prompts = prompts if prompts is not None else {}
    _patch_sdk(monkeypatch, _echo_runner(prompts))
    store = WorkflowRunStore(tmp_path)
    result = asyncio.run(wf._execute(_GatedReg(), "gated", {"topic": "ai"}, run_store=store))
    assert result["paused"] is True and result["paused_step"] == "analyze"
    return store, result["run_id"]


# --- engine-level resume knobs ------------------------------------------------


def test_engine_seed_outputs_skip_completed_steps():
    prompts: dict[str, str] = {}

    async def run_step(subagent, prompt, sid):
        prompts[sid] = prompt
        return prompt

    # gather is pre-seeded → never dispatched; analyze runs with its gate bypassed.
    res = asyncio.run(
        execute_workflow(
            GATED,
            {"topic": "ai"},
            run_step=run_step,
            gate_check=lambda s: "pause" if s.get("gate") == "human" else None,
            seed_outputs={"gather": "prior gather"},
            skip_gate={"analyze"},
        )
    )
    assert "gather" not in prompts  # already done — not re-run
    assert prompts["analyze"] == "analyze:\nprior gather"  # templated off the seeded output
    assert res["failed"] == []


def test_engine_prefailed_records_error_inline_and_dependents_inherit():
    async def run_step(subagent, prompt, sid):
        return prompt

    res = asyncio.run(
        execute_workflow(
            GATED,
            {"topic": "ai"},
            run_step=run_step,
            seed_outputs={"gather": "g"},
            prefailed={"analyze": "rejected by operator"},
        )
    )
    assert res["failed"] == ["analyze"]
    assert res["steps"]["analyze"] == "rejected by operator"
    # write is a dependent — its prompt inherited the error text (inline-failure semantics).
    assert res["steps"]["write"] == "write up:\nrejected by operator"


def test_engine_prompt_override_runs_verbatim():
    prompts: dict[str, str] = {}

    async def run_step(subagent, prompt, sid):
        prompts[sid] = prompt
        return prompt

    res = asyncio.run(
        execute_workflow(
            GATED,
            {"topic": "ai"},
            run_step=run_step,
            seed_outputs={"gather": "g"},
            skip_gate={"analyze"},
            prompt_overrides={"analyze": "CUSTOM ANALYSIS"},
        )
    )
    assert prompts["analyze"] == "CUSTOM ANALYSIS"  # verbatim, not the template
    assert prompts["write"] == "write up:\nCUSTOM ANALYSIS"  # downstream sees the edited output
    assert res["output"] == "write up:\nCUSTOM ANALYSIS"


# --- _resume orchestration + run-state ----------------------------------------


def test_approve_runs_gated_step_with_original_prompt_and_completes(tmp_path, monkeypatch):
    prompts: dict[str, str] = {}
    store, run_id = _pause_gated(monkeypatch, tmp_path, prompts)
    prompts.clear()  # only track what resume dispatches

    result = asyncio.run(wf._resume(_GatedReg(), run_id, "approve", run_store=store))

    # gather was already done pre-pause → NOT re-run; analyze + write ran.
    assert "gather" not in prompts
    assert prompts["analyze"] == "analyze:\nresearch ai"  # original prompt, prior output substituted
    assert prompts["write"] == "write up:\nanalyze:\nresearch ai"
    assert result["failed"] == []
    assert result["run_id"] == run_id
    state = store.load(run_id)
    assert state["status"] == STATUS_DONE
    assert state["pending_step"] is None


def test_edit_runs_edited_prompt_and_downstream_sees_it(tmp_path, monkeypatch):
    prompts: dict[str, str] = {}
    store, run_id = _pause_gated(monkeypatch, tmp_path, prompts)
    prompts.clear()

    result = asyncio.run(wf._resume(_GatedReg(), run_id, "edit", edits={"prompt": "DO THIS INSTEAD"}, run_store=store))

    assert prompts["analyze"] == "DO THIS INSTEAD"
    assert "DO THIS INSTEAD" in prompts["write"]  # downstream step sees the edited output
    assert "DO THIS INSTEAD" in result["output"]
    assert store.load(run_id)["status"] == STATUS_DONE


def test_reject_marks_step_failed_dependents_inherit_error(tmp_path, monkeypatch):
    prompts: dict[str, str] = {}
    store, run_id = _pause_gated(monkeypatch, tmp_path, prompts)
    prompts.clear()

    result = asyncio.run(wf._resume(_GatedReg(), run_id, "reject", run_store=store))

    assert result["failed"] == ["analyze"]
    assert result["steps"]["analyze"] == "rejected by operator"
    assert "analyze" not in prompts  # the rejected step never ran a subagent
    assert "rejected by operator" in prompts["write"]  # dependent inherited the error
    state = store.load(run_id)
    assert state["status"] == STATUS_FAILED
    assert state["step_outputs"]["analyze"] == "rejected by operator"


def test_resume_survives_server_restart(tmp_path, monkeypatch):
    # Pause with one store, then resume through a FRESH store over the same dir — the
    # paused state is read off disk (as a new process would after a restart).
    _pause_store, run_id = _pause_gated(monkeypatch, tmp_path)
    reopened = WorkflowRunStore(tmp_path)
    assert reopened.load(run_id)["status"] == STATUS_PAUSED

    result = asyncio.run(wf._resume(_GatedReg(), run_id, "approve", run_store=reopened))
    assert result["failed"] == []
    assert reopened.load(run_id)["status"] == STATUS_DONE


def test_resume_rejects_unknown_action_and_non_paused_run(tmp_path, monkeypatch):
    store, run_id = _pause_gated(monkeypatch, tmp_path)
    try:
        asyncio.run(wf._resume(_GatedReg(), run_id, "bogus", run_store=store))
        raise AssertionError("expected ValueError for unknown action")
    except ValueError as exc:
        assert "bogus" in str(exc)

    # Approve it to completion, then a second resume must fail (no longer paused).
    asyncio.run(wf._resume(_GatedReg(), run_id, "approve", run_store=store))
    try:
        asyncio.run(wf._resume(_GatedReg(), run_id, "approve", run_store=store))
        raise AssertionError("expected ValueError for a non-paused run")
    except ValueError as exc:
        assert "not paused" in str(exc)


# --- Pending Gates listing (GET /api/plugins/workflows/runs) -------------------


def test_list_paused_runs_renders_prompt_with_inputs_and_prior_outputs(tmp_path, monkeypatch):
    store, run_id = _pause_gated(monkeypatch, tmp_path)

    runs = wf._list_paused_runs(_GatedReg(), run_store=store)
    assert len(runs) == 1
    view = runs[0]
    assert view["run_id"] == run_id
    assert view["recipe_name"] == "gated"
    assert view["paused_step"] == "analyze"
    # The RENDERED prompt — inputs + prior outputs substituted, never raw template syntax.
    assert view["prompt"] == "analyze:\nresearch ai"
    assert "{{" not in view["prompt"]
    assert view["step_outputs"] == {"gather": "research ai"}
    assert view["created_at"] and view["updated_at"]


def test_list_paused_runs_is_empty_when_none(tmp_path):
    assert wf._list_paused_runs(_GatedReg(), run_store=WorkflowRunStore(tmp_path)) == []


def test_resolved_run_drops_out_of_the_paused_listing(tmp_path, monkeypatch):
    store, run_id = _pause_gated(monkeypatch, tmp_path)
    assert len(wf._list_paused_runs(_GatedReg(), run_store=store)) == 1
    asyncio.run(wf._resume(_GatedReg(), run_id, "approve", run_store=store))
    # Once completed the run is no longer paused → the queue is empty again.
    assert wf._list_paused_runs(_GatedReg(), run_store=store) == []


def test_resume_edit_without_prompt_leaves_run_paused(tmp_path):
    """Blocker pin (slice-3 QA panel): an edit action missing edits.prompt must fail
    BEFORE the run flips to running — the run stays paused and resumable, never
    orphaned in a running state it can't leave."""
    import asyncio

    import pytest

    from plugins.workflows import _resume
    from plugins.workflows.run_state import WorkflowRunStore

    store = WorkflowRunStore(tmp_path)
    run_id = store.start("r", {"x": "1"})
    store.pause("analyze")

    class _Reg:
        def get(self, name):  # never reached — validation fires first
            raise AssertionError("registry consulted before validation")

    with pytest.raises(ValueError, match="edits.prompt"):
        asyncio.run(_resume(_Reg(), run_id, "edit", edits={}, run_store=store))
    assert store.load(run_id)["status"] == "paused"  # still resumable


def test_resume_edit_null_prompt_leaves_run_paused(tmp_path):
    """#2143: `edits.prompt = null` (JSON null → None) must fail up front like a
    missing prompt. The old guard did `str(edits.get("prompt","")).strip()`, and
    `str(None)` == "None" (truthy) slipped None past it — the run then flipped to
    running and a SECOND check raised too late, orphaning it. Pin: raises BEFORE the
    flip (registry never consulted) and the run stays paused/resumable."""
    import asyncio

    import pytest

    from plugins.workflows import _resume
    from plugins.workflows.run_state import WorkflowRunStore

    store = WorkflowRunStore(tmp_path)
    run_id = store.start("r", {"x": "1"})
    store.pause("analyze")

    class _Reg:
        def get(self, name):  # must not be reached — validation fires first
            raise AssertionError("registry consulted before validation (run would orphan)")

    with pytest.raises(ValueError, match="edits.prompt"):
        asyncio.run(_resume(_Reg(), run_id, "edit", edits={"prompt": None}, run_store=store))
    assert store.load(run_id)["status"] == "paused"  # not orphaned in `running`
