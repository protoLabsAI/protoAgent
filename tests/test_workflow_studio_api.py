"""The Studio's run surface: background starts, the live per-step record, run
history + retention, live validation, and the resume precheck (#2143 family)."""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest

import plugins.workflows as wf
from plugins.workflows.run_state import (
    STATUS_DONE,
    STATUS_FAILED,
    STATUS_PAUSED,
    STATUS_RUNNING,
    WorkflowRunStore,
)

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


async def _drain_bg():
    await asyncio.gather(*list(wf._BG_TASKS), return_exceptions=True)


# --- background start (the Studio's live-run shape) ---------------------------


async def test_start_background_returns_immediately_with_live_step_state(tmp_path, monkeypatch):
    release = asyncio.Event()

    async def fake_run(subagent, prompt, description=""):
        await release.wait()
        return f"out:{description.rsplit(':', 1)[-1]}"

    _patch_sdk(monkeypatch, fake_run)
    store = WorkflowRunStore(tmp_path / ".runs")
    events: list[tuple[str, dict]] = []

    run_id = await wf._start_background(
        _FakeReg(), "demo", {"topic": "ai"}, notify=lambda e, d: events.append((e, d)), run_store=store
    )

    # returned before any step finished; the record already shows the graph + a running step
    await asyncio.sleep(0.05)
    state = WorkflowRunStore(tmp_path / ".runs").load(run_id)
    assert state["status"] == STATUS_RUNNING
    assert [s["id"] for s in state["steps"]] == ["gather", "brief"]
    assert state["step_meta"]["gather"]["status"] == STATUS_RUNNING
    assert state["step_meta"]["gather"]["started_at"]
    assert "brief" not in state["step_meta"]  # not dispatched yet — deps unmet

    release.set()
    await _drain_bg()

    state = WorkflowRunStore(tmp_path / ".runs").load(run_id)
    assert state["status"] == STATUS_DONE
    assert state["output"] == "out:brief"
    assert state["failed"] == []
    assert state["step_meta"]["brief"]["status"] == STATUS_DONE
    assert state["step_meta"]["brief"]["finished_at"]
    assert isinstance(state["step_meta"]["brief"].get("seconds"), (int, float))
    assert events == [("run-finished", {"run_id": run_id, "recipe": "demo", "status": STATUS_DONE})]


async def test_start_background_validation_fails_caller_and_mints_no_run(tmp_path, monkeypatch):
    async def fake_run(subagent, prompt, description=""):
        return "x"

    _patch_sdk(monkeypatch, fake_run)
    store = WorkflowRunStore(tmp_path / ".runs")
    with pytest.raises(ValueError, match="missing required"):
        await wf._start_background(_FakeReg(), "demo", {}, run_store=store)
    assert store.list_runs() == []


async def test_background_failure_lands_in_record_and_notify(tmp_path, monkeypatch):
    async def fake_run(subagent, prompt, description=""):
        raise RuntimeError("boom")

    _patch_sdk(monkeypatch, fake_run)
    store = WorkflowRunStore(tmp_path / ".runs")
    events: list[tuple[str, dict]] = []
    run_id = await wf._start_background(
        _FakeReg(), "demo", {"topic": "ai"}, notify=lambda e, d: events.append((e, d)), run_store=store
    )
    await _drain_bg()
    state = WorkflowRunStore(tmp_path / ".runs").load(run_id)
    assert state["status"] == STATUS_FAILED
    # the engine records the error inline; both steps carry failed meta (brief inherits)
    assert state["step_meta"]["gather"]["status"] == STATUS_FAILED
    assert "boom" in state["step_outputs"]["gather"]
    assert events[-1][0] == "run-finished" and events[-1][1]["status"] == STATUS_FAILED


async def test_background_gate_pause_notifies_and_stays_resumable(tmp_path, monkeypatch):
    gated = {
        **RECIPE,
        "name": "gated",
        "steps": [
            RECIPE["steps"][0],
            {**RECIPE["steps"][1], "gate": "human"},
        ],
    }

    async def fake_run(subagent, prompt, description=""):
        return "gathered"

    _patch_sdk(monkeypatch, fake_run)
    store = WorkflowRunStore(tmp_path / ".runs")
    events: list[tuple[str, dict]] = []
    run_id = await wf._start_background(
        _FakeReg(gated), "gated", {"topic": "ai"}, notify=lambda e, d: events.append((e, d)), run_store=store
    )
    await _drain_bg()
    state = WorkflowRunStore(tmp_path / ".runs").load(run_id)
    assert state["status"] == STATUS_PAUSED
    assert state["pending_step"] == "brief"
    assert events == [("run-paused", {"run_id": run_id, "recipe": "gated", "paused_step": "brief"})]


# --- history + retention ------------------------------------------------------


def test_recent_summarizes_newest_first(tmp_path):
    store = WorkflowRunStore(tmp_path / ".runs")
    a = store.start("one", {}, steps=RECIPE["steps"])
    store.step_done("gather", "out")
    store.finish(STATUS_DONE, {"output": "o", "failed": [], "timings": {"gather": 1.0}})
    b = store.start("two", {}, steps=RECIPE["steps"])
    store.pause("brief", {})

    recent = store.recent()
    # [b, a] holds even when both runs land in the same clock tick (Windows):
    # the write seq tie-breaks identical updated_at values (#2883).
    assert [r["run_id"] for r in recent] == [b, a]
    assert recent[0]["status"] == STATUS_PAUSED and recent[0]["pending_step"] == "brief"
    assert recent[1]["steps_total"] == 2 and recent[1]["steps_done"] == 1
    assert "step_outputs" not in recent[0]  # summaries, not payloads


def _freeze_updated_at(store, run_ids):
    """Force the same-tick updated_at collision the Windows flake needs, on any
    platform — seq (already persisted, later write = higher) must break the tie."""
    for rid in run_ids:
        state = store.load(rid)
        state["updated_at"] = "2026-01-01T00:00:00+00:00"
        store.path(rid).write_text(json.dumps(state), encoding="utf-8")


def test_recent_tie_breaks_identical_updated_at_by_write_order(tmp_path):
    store = WorkflowRunStore(tmp_path / ".runs")
    a = store.start("one", {})
    store.finish(STATUS_DONE, {"output": "", "failed": []})
    b = store.start("two", {})
    store.finish(STATUS_DONE, {"output": "", "failed": []})
    _freeze_updated_at(store, [a, b])

    assert [r["run_id"] for r in WorkflowRunStore(tmp_path / ".runs").recent()] == [b, a]


def test_paused_tie_breaks_identical_updated_at_by_write_order(tmp_path):
    store = WorkflowRunStore(tmp_path / ".runs")
    a = store.start("one", {}, steps=RECIPE["steps"])
    store.pause("gather", {})
    b = store.start("two", {}, steps=RECIPE["steps"])
    store.pause("brief", {})
    _freeze_updated_at(store, [a, b])

    assert [r["run_id"] for r in WorkflowRunStore(tmp_path / ".runs").paused()] == [b, a]


def test_prune_removes_only_oldest_terminal_runs(tmp_path):
    store = WorkflowRunStore(tmp_path / ".runs")
    terminal = []
    for i in range(4):
        rid = store.start(f"r{i}", {})
        store.finish(STATUS_DONE, {"output": "", "failed": []})
        terminal.append(rid)
    paused = store.start("parked", {})
    store.pause("s", {})

    assert store.prune(keep=2) == 2
    left = set(store.list_runs())
    assert paused in left  # resumable state is never pruned
    assert set(terminal[2:]) <= left and not (set(terminal[:2]) & left)
    assert store.prune(keep=2) == 0


# --- resume precheck (#2143: never flip state on a doomed resume) -------------


def test_resume_precheck_rejects_without_flipping(tmp_path):
    store = WorkflowRunStore(tmp_path / ".runs")
    run_id = store.start("demo", {"topic": "ai"})
    store.pause("brief", {"gather": "out"})

    for action, edits in [("nonsense", None), ("edit", {}), ("edit", {"prompt": None}), ("edit", {"prompt": "  "})]:
        with pytest.raises(ValueError):
            wf._resume_precheck(store, run_id, action, edits)
        assert WorkflowRunStore(tmp_path / ".runs").load(run_id)["status"] == STATUS_PAUSED

    with pytest.raises(ValueError, match="no run"):
        wf._resume_precheck(store, "ghost", "approve", None)

    ok = wf._resume_precheck(store, run_id, "edit", {"prompt": "new"})
    assert ok["pending_step"] == "brief"


# --- the routes, mounted as register() mounts them ----------------------------


class _FakeRegistry:
    def __init__(self):
        from fastapi import FastAPI

        self.app = FastAPI()
        self.config = {"max_runs": 200}

    def register_router(self, router, prefix):
        self.app.include_router(router, prefix=prefix)

    def register_tools(self, tools):
        self.tools = tools

    def register_workflow_dir(self, d):
        pass

    def emit(self, topic, data):
        pass


@pytest.fixture
def client(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    from runtime.state import STATE

    async def fake_run(subagent, prompt, description=""):
        return "ok"

    _patch_sdk(monkeypatch, fake_run, workflow_dir=str(tmp_path / "wfdir"))
    # register() publishes onto runtime STATE — restore it so tests that assert the
    # no-plugin default (workflow slash-command) aren't poisoned by this fixture.
    prev = (getattr(STATE, "workflow_registry", None), getattr(STATE, "workflow_run", None))
    registry = _FakeRegistry()
    wf.register(registry)
    yield TestClient(registry.app)
    STATE.workflow_registry, STATE.workflow_run = prev


def test_runs_all_route_is_not_captured_as_a_run_id(client, tmp_path):
    # /runs/all must resolve to the history listing, not 404 as run_id="all"
    r = client.get("/api/plugins/workflows/runs/all")
    assert r.status_code == 200
    assert r.json() == {"runs": []}
    assert client.get("/api/plugins/workflows/runs/ghost-id").status_code == 404


def test_recipe_route_returns_the_full_document(client):
    gated = {
        "name": "editme",
        "version": 1,
        "steps": [{"id": "a", "subagent": "researcher", "prompt": "p {{inputs.x}}", "gate": "human"}],
        "inputs": [{"name": "x", "required": True}],
        "output": "{{steps.a.output}}",
    }
    assert client.post("/api/plugins/workflows/save", json=gated).status_code == 200
    got = client.get("/api/plugins/workflows/editme/recipe").json()["recipe"]
    assert got["steps"][0]["prompt"] == "p {{inputs.x}}"
    assert got["steps"][0]["gate"] == "human"
    assert got["output"] == "{{steps.a.output}}"
    assert client.get("/api/plugins/workflows/ghost/recipe").status_code == 404
    # the summary list marks the gate for the DAG lanes
    listed = client.get("/api/plugins/workflows/list").json()["workflows"]
    me = next(w for w in listed if w["name"] == "editme")
    assert me["steps"][0].get("gate") == "human"


def test_validate_route_returns_errors_as_data(client):
    bad = {"name": "x", "steps": [{"id": "a", "subagent": "nope", "prompt": "p", "depends_on": ["z"]}]}
    errs = client.post("/api/plugins/workflows/validate", json=bad).json()["errors"]
    assert any("unknown step 'z'" in e for e in errs)
    good = {"name": "x", "steps": [{"id": "a", "subagent": "researcher", "prompt": "p"}]}
    assert client.post("/api/plugins/workflows/validate", json=good).json()["errors"] == []


def test_start_route_then_poll_run_record(client):
    save = client.post(
        "/api/plugins/workflows/save",
        json={"name": "quick", "version": 1, "steps": [{"id": "a", "subagent": "researcher", "prompt": "p"}]},
    )
    assert save.status_code == 200
    r = client.post("/api/plugins/workflows/quick/start", json={"inputs": {}})
    assert r.status_code == 200
    run_id = r.json()["run_id"]
    # the DAG runs detached on the app loop — poll the record like the Studio does
    import time

    state = {}
    for _ in range(100):
        state = client.get(f"/api/plugins/workflows/runs/{run_id}").json()
        if state.get("status") != STATUS_RUNNING:
            break
        time.sleep(0.02)
    assert state["status"] == STATUS_DONE
    assert state["output"] == "ok"
    history = client.get("/api/plugins/workflows/runs/all").json()["runs"]
    assert history[0]["run_id"] == run_id
    assert client.post("/api/plugins/workflows/missing/start", json={}).status_code == 400
