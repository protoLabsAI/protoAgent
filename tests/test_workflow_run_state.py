"""Tests for workflow run-state persistence (WorkflowRunStore + the _execute audit trail)."""

from __future__ import annotations

import asyncio
import json
import uuid
from types import SimpleNamespace

import plugins.workflows as wf
from plugins.workflows.run_state import STATUS_DONE, STATUS_FAILED, STATUS_RUNNING, STATUS_SEEDED, WorkflowRunStore
import pytest

pytestmark = pytest.mark.platform_sensitive

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
    async def run_subagent(subagent_type, prompt, description="", **_sdk_kwargs):
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

    async def run_subagent(subagent_type, prompt, description="", **_sdk_kwargs):
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

    async def run_subagent(subagent_type, prompt, description="", **_sdk_kwargs):
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
    async def run_subagent(subagent_type, prompt, description="", **_sdk_kwargs):
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
    async def run_subagent(subagent_type, prompt, description="", **_sdk_kwargs):
        return "out"

    _patch_sdk(monkeypatch, run_subagent, workflow_dir=str(tmp_path / "wfdir"))
    result = asyncio.run(wf._execute(_FakeReg(), "demo", {"topic": "ai"}))
    run_file = tmp_path / "wfdir" / ".runs" / f"{result['run_id']}.json"
    assert run_file.exists()
    assert json.loads(run_file.read_text(encoding="utf-8"))["status"] == STATUS_DONE


def test_validation_failure_creates_no_run_file(tmp_path, monkeypatch):
    async def run_subagent(subagent_type, prompt, description="", **_sdk_kwargs):
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


def test_step_outputs_drop_the_subagent_completed_header(tmp_path, monkeypatch):
    # #3946: `run_subagent` returns `[<type> completed: <description>]\n\n<body>` (graph/agent
    # run_manual_subagent) — a tool-result marker for a lead model. In a workflow it leaked
    # into the reply as a raw "[researcher completed: workflow demo:brief]" first line and
    # into every downstream prompt. The clean-finish header is dropped; warning headers stay.
    prompts: dict[str, str] = {}

    async def run_subagent(subagent_type, prompt, description="", **_sdk_kwargs):
        step = description.rsplit(":", 1)[-1]
        prompts[step] = prompt
        return f"[{subagent_type} completed: {description}]\n\n<{step}-out>"

    _patch_sdk(monkeypatch, run_subagent)
    store = WorkflowRunStore(tmp_path)
    result = asyncio.run(wf._execute(_FakeReg(), "demo", {"topic": "ai"}, run_store=store))

    assert result["output"] == "<brief-out>"
    assert "completed:" not in prompts["brief"] and "<gather-out>" in prompts["brief"]
    assert store.load(result["run_id"])["step_outputs"] == {"gather": "<gather-out>", "brief": "<brief-out>"}


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("[researcher completed: workflow demo:s]\n\nbody", "body"),
        ("[researcher completed: workflow demo:s] -- no output produced.", "(no output produced)"),
        # Warning headers carry real signal (treat the lane as a Gap) — kept verbatim.
        (
            "[researcher hard-stopped at max_turns: workflow demo:s — PARTIAL output; "
            "unverified remainder is a Gap]\n\nbody",
            None,
        ),
        ("[researcher ended without its deliverable: workflow demo:s] -- no output produced; x", None),
        # Another subagent's / description's header is not ours to strip.
        ("[coder completed: workflow demo:s]\n\nbody", None),
        ("plain answer", None),
    ],
)
def test_strip_completed_header(raw, expected):
    out = wf._strip_completed_header(raw, "researcher", "workflow demo:s")
    assert out == (raw if expected is None else expected)


def test_workflows_plugin_config_section_does_not_collide_with_core(caplog):
    # #3946: the manifest claimed `workflows`, core's reserved built-in section (the
    # `workflows.dir` recipe root) — so its `max_runs` config never bound and every config
    # load logged "config_section 'workflows' collides with a built-in". It has its own now.
    import logging
    from pathlib import Path

    from graph.plugins.pconfig import _RESERVED_SECTIONS, discover_plugin_config

    assert "workflows" in _RESERVED_SECTIONS  # core still owns `workflows:` — not unreserved
    plugins_root = Path(wf.__file__).resolve().parent.parent
    with caplog.at_level(logging.WARNING, logger="protoagent.plugins"):
        schemas = discover_plugin_config([plugins_root], {"workflows"})
    mine = [s for s in schemas if s.plugin_id == "workflows"]
    assert len(mine) == 1 and mine[0].section not in _RESERVED_SECTIONS
    assert mine[0].defaults.get("max_runs") == 200
    assert not [r for r in caplog.records if "collides with a built-in" in r.getMessage()]


# --- a cancelled run is recorded as cancelled, not failed (#3957) ---------------


def _blocking_gather(started: asyncio.Event):
    async def run_subagent(subagent_type, prompt, description="", **_sdk_kwargs):
        if description.endswith(":gather"):
            started.set()
            await asyncio.sleep(3600)
        return "brief-out"

    return run_subagent


def test_a_cancelled_run_is_recorded_cancelled_with_its_in_flight_step(tmp_path, monkeypatch):
    """A run stopped from outside — its turn ended (CancelTask, the stall guard) — was
    recorded ``failed``, indistinguishable from a step that broke. It is ``cancelled``,
    and the step that was running is marked cancelled rather than left ``running`` on a
    terminal record."""

    async def scenario():
        started = asyncio.Event()
        _patch_sdk(monkeypatch, _blocking_gather(started))
        store = WorkflowRunStore(tmp_path)
        task = asyncio.create_task(wf._execute(_FakeReg(), "demo", {"topic": "ai"}, run_store=store))
        await started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        return store

    store = asyncio.run(scenario())
    state = store.load(store.run_id)
    assert state["status"] == "cancelled"
    assert state["step_meta"]["gather"]["status"] == "cancelled"
    assert state["step_meta"]["gather"].get("finished_at")


def test_a_cancelled_run_is_terminal_for_pruning(tmp_path):
    from plugins.workflows.run_state import STATUS_CANCELLED, TERMINAL

    assert STATUS_CANCELLED in TERMINAL
    store = WorkflowRunStore(tmp_path)
    for _ in range(3):
        store.start("demo", {})
        store.finish(STATUS_CANCELLED)
    assert store.prune(keep=1) == 2


def test_a_run_that_raises_is_still_recorded_failed(tmp_path, monkeypatch):
    """Only a cancel is ``cancelled`` — a run that crashes outside the engine's inline
    step-failure handling is still ``failed``."""

    async def run_subagent(subagent_type, prompt, description="", **_sdk_kwargs):
        return "out"

    _patch_sdk(monkeypatch, run_subagent)

    def _boom(*_a, **_k):
        raise RuntimeError("engine crashed")

    monkeypatch.setattr(wf, "execute_workflow", _boom)
    store = WorkflowRunStore(tmp_path)
    with pytest.raises(RuntimeError):
        asyncio.run(wf._execute(_FakeReg(), "demo", {"topic": "ai"}, run_store=store))
    assert store.load(store.run_id)["status"] == STATUS_FAILED


# ── #5411: a `running` record left behind by a dead process ──────────────────────────


def _backdate(store: WorkflowRunStore, run_id: str, days: float) -> None:
    """Rewrite a record's ``updated_at`` as if its last write were ``days`` ago."""
    from datetime import datetime, timedelta, timezone

    path = store.path(run_id)
    data = json.loads(path.read_text(encoding="utf-8"))
    data["updated_at"] = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    path.write_text(json.dumps(data), encoding="utf-8")


def _orphan(tmp_path, days: float, *, paused: bool = False) -> str:
    """A record written by a store that is gone (the process died), ``days`` old."""
    dead = WorkflowRunStore(tmp_path)
    run_id = dead.start("demo", {}, steps=[{"id": "gather", "subagent": "researcher"}])
    dead.step_started("gather")
    if paused:
        dead.pause("gather")
    _backdate(dead, run_id, days)
    return run_id


def test_prune_marks_a_stale_running_run_interrupted(tmp_path):
    from plugins.workflows.run_state import INTERRUPTED_ERROR

    stale = _orphan(tmp_path, days=30)
    before = json.loads((tmp_path / f"{stale}.json").read_text(encoding="utf-8"))
    store = WorkflowRunStore(tmp_path)

    assert store.prune(keep=10) == 0  # reclassified, not deleted: well under `keep`

    state = store.load(stale)
    assert state["status"] == STATUS_FAILED
    assert state["interrupted"] is True
    assert state["error"] == INTERRUPTED_ERROR
    assert state["step_meta"]["gather"]["status"] == STATUS_FAILED
    assert state["step_meta"]["gather"].get("finished_at")
    # Bumped like any other write, and the rest of the record is intact.
    assert state["updated_at"] > before["updated_at"]
    assert state["seq"] > before["seq"]
    assert state["recipe_name"] == "demo" and state["created_at"] == before["created_at"]
    # Idempotent: an already-interrupted record is plain history on the next prune.
    again = state["updated_at"]
    assert store.prune(keep=10) == 0
    assert store.load(stale)["updated_at"] == again


def test_prune_leaves_fresh_running_and_all_paused_runs_alone(tmp_path):
    fresh = _orphan(tmp_path, days=1)
    old_paused = _orphan(tmp_path, days=400, paused=True)
    new_paused = _orphan(tmp_path, days=0, paused=True)
    store = WorkflowRunStore(tmp_path)
    for _ in range(3):
        store.start("demo", {})
        store.finish(STATUS_DONE)
    live = store.start("demo", {})  # the run being started when prune is called
    files = {rid: (tmp_path / f"{rid}.json").read_text(encoding="utf-8") for rid in (fresh, old_paused, new_paused)}

    assert store.prune(keep=1) == 2  # only the terminal runs beyond `keep`

    assert store.load(fresh)["status"] == STATUS_RUNNING
    assert store.load(live)["status"] == STATUS_RUNNING
    assert store.load(old_paused)["status"] == "paused"
    assert store.load(old_paused)["pending_step"] == "gather"
    assert store.load(new_paused)["status"] == "paused"
    for rid, text in files.items():
        assert (tmp_path / f"{rid}.json").read_text(encoding="utf-8") == text  # not rewritten


def test_prune_stale_threshold_is_a_keyword_argument(tmp_path):
    run_id = _orphan(tmp_path, days=1)
    store = WorkflowRunStore(tmp_path)
    store.prune(keep=10)
    assert store.load(run_id)["status"] == STATUS_RUNNING  # inside the 7-day default
    store.prune(keep=10, stale_running_s=3600)
    assert store.load(run_id)["status"] == STATUS_FAILED


def test_prune_never_reclassifies_the_stores_own_run(tmp_path):
    store = WorkflowRunStore(tmp_path)
    run_id = store.start("demo", {})
    _backdate(store, run_id, days=30)  # a live run whose last write is old
    store.prune(keep=10)
    assert store.load(run_id)["status"] == STATUS_RUNNING


def test_reclassified_runs_are_pruned_under_the_keep_rule(tmp_path):
    stale = [_orphan(tmp_path, days=30 + i) for i in range(3)]
    store = WorkflowRunStore(tmp_path)
    store.start("demo", {})
    store.finish(STATUS_DONE)
    done = store.run_id
    _backdate(store, done, days=60)  # the oldest terminal record on disk

    # 3 interrupted + 1 done = 4 terminal; keep 2 → the 2 oldest go, `done` first.
    assert store.prune(keep=2) == 2

    left = store.list_runs()
    assert done not in left
    assert len(left) == 2 and set(left) <= set(stale)
    assert all(store.load(rid)["status"] == STATUS_FAILED for rid in left)


def test_prune_skips_records_it_cannot_read_or_rewrite(tmp_path, monkeypatch, caplog):
    import plugins.workflows.run_state as rs

    stale = _orphan(tmp_path, days=30)
    (tmp_path / "corrupt.json").write_text("{not json", encoding="utf-8")
    no_stamp = _orphan(tmp_path, days=30)
    path = tmp_path / f"{no_stamp}.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    data["updated_at"] = data["created_at"] = "not-a-date"
    path.write_text(json.dumps(data), encoding="utf-8")
    store = WorkflowRunStore(tmp_path)
    for _ in range(2):
        store.start("demo", {})
        store.finish(STATUS_DONE)

    def boom(*_a, **_k):
        raise OSError("disk full")

    monkeypatch.setattr(rs, "atomic_write", boom)
    with caplog.at_level("WARNING", logger=rs.__name__):
        assert store.prune(keep=1) == 1  # did not raise; the unwritable record is not terminal

    assert json.loads((tmp_path / f"{stale}.json").read_text(encoding="utf-8"))["status"] == STATUS_RUNNING
    assert store.load(no_stamp)["status"] == STATUS_RUNNING  # unknown age is not stale
    assert (tmp_path / "corrupt.json").exists()
    assert "corrupt" in caplog.text and stale in caplog.text
