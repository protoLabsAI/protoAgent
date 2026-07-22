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
        {
            "id": "brief",
            "subagent": "researcher",
            "depends_on": ["gather"],
            "prompt": "write up:\n{{steps.gather.output}}",
        },
    ],
    "output": "{{steps.brief.output}}",
}

# A recipe with a `gate: human` step — the gated step (`analyze`) must pause for
# operator approval before its subagent is spawned (F2, ADR 0002).
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
    ],
    "output": "{{steps.analyze.output}}",
}


def test_validate_accepts_valid_recipe():
    assert validate_recipe(VALID, known_subagents={"researcher"}) == []


def test_validate_catches_structural_errors():
    assert "missing 'name'" in validate_recipe({"steps": [{"id": "a", "subagent": "researcher", "prompt": "x"}]})
    assert any("non-empty list" in e for e in validate_recipe({"name": "x"}))
    dup = {
        "name": "x",
        "steps": [
            {"id": "a", "subagent": "researcher", "prompt": "p"},
            {"id": "a", "subagent": "researcher", "prompt": "p"},
        ],
    }
    assert any("duplicate step id" in e for e in validate_recipe(dup))


def test_validate_catches_dep_and_cycle_and_subagent():
    bad_dep = {"name": "x", "steps": [{"id": "a", "subagent": "researcher", "prompt": "p", "depends_on": ["z"]}]}
    assert any("unknown step 'z'" in e for e in validate_recipe(bad_dep))
    cycle = {
        "name": "x",
        "steps": [
            {"id": "a", "subagent": "researcher", "prompt": "p", "depends_on": ["b"]},
            {"id": "b", "subagent": "researcher", "prompt": "p", "depends_on": ["a"]},
        ],
    }
    assert any("cycle" in e for e in validate_recipe(cycle))
    unknown_sub = {"name": "x", "steps": [{"id": "a", "subagent": "nope", "prompt": "p"}]}
    assert any("unknown subagent" in e for e in validate_recipe(unknown_sub, known_subagents={"researcher"}))


def test_validate_catches_bad_template_refs():
    bad = {
        "name": "x",
        "inputs": [{"name": "topic"}],
        "steps": [
            {"id": "a", "subagent": "researcher", "prompt": "{{inputs.missing}} {{steps.ghost.output}}"},
        ],
    }
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

    fanout = {
        "name": "f",
        "steps": [
            {"id": "a", "subagent": "researcher", "prompt": "p"},
            {"id": "b", "subagent": "researcher", "prompt": "p"},
            {"id": "c", "subagent": "researcher", "prompt": "p"},
        ],
    }
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


# --- Step-level gate: human (F2) ---------------------------------------------


def test_validate_accepts_human_gate():
    assert validate_recipe(GATED, known_subagents={"researcher"}) == []


def test_validate_rejects_unsupported_gate():
    bad = {
        "name": "x",
        "steps": [{"id": "a", "subagent": "researcher", "prompt": "p", "gate": "robot"}],
    }
    errs = validate_recipe(bad, known_subagents={"researcher"})
    assert any("unsupported gate" in e and "robot" in e for e in errs)
    # A truthy-but-wrong value is still rejected (only the literal 'human' is accepted).
    assert any("unsupported gate" in e for e in validate_recipe({**bad, "steps": [{"id": "a", "subagent": "researcher", "prompt": "p", "gate": True}]}, known_subagents={"researcher"}))


def test_execute_pauses_before_gated_step_is_spawned():
    """The gated step's subagent must never run — pause happens first (no wasted work)."""
    spawned = []

    async def run_step(subagent, prompt, sid):
        spawned.append(sid)
        return f"<{sid}-out>"

    def gate_check(step):
        return "pause" if step.get("gate") == "human" else None

    def pause_fn(step_id, done):
        return "run-abc"

    res = asyncio.run(
        execute_workflow(GATED, {"topic": "ai"}, run_step=run_step, gate_check=gate_check, pause_fn=pause_fn)
    )
    # gather (ungated) ran; analyze (gated) paused before spawning.
    assert spawned == ["gather"]
    assert res == {"paused": True, "paused_step": "analyze", "run_id": "run-abc", "steps": {"gather": "<gather-out>"}}


def test_execute_pauses_at_the_very_first_step_when_gated():
    """A gated first step pauses immediately — zero prior work."""
    spawned = []

    async def run_step(subagent, prompt, sid):
        spawned.append(sid)
        return sid

    recipe = {
        "name": "g",
        "steps": [{"id": "first", "subagent": "researcher", "prompt": "p", "gate": "human"}],
    }
    res = asyncio.run(
        execute_workflow(
            recipe,
            {},
            run_step=run_step,
            gate_check=lambda s: "pause" if s.get("gate") == "human" else None,
            pause_fn=lambda sid, done: "rid",
        )
    )
    assert spawned == []  # nothing spawned at all
    assert res == {"paused": True, "paused_step": "first", "run_id": "rid", "steps": {}}


def test_execute_multiple_gated_steps_pause_in_turn():
    """Sequential gated steps pause one at a time — not all at once. Approving 'a'
    lets it run; only then does the downstream gated 'b' become ready and pause."""
    spawned = []
    paused_with = []

    async def run_step(subagent, prompt, sid):
        spawned.append(sid)
        return f"<{sid}>"

    # Simulates a resume where 'a' was already approved (returns None) but 'b' is not.
    def gate_check(step):
        return "pause" if step.get("id") == "b" else None

    def pause_fn(step_id, done):
        paused_with.append((step_id, dict(done)))
        return "run-seq"

    recipe = {
        "name": "seq",
        "steps": [
            {"id": "a", "subagent": "researcher", "prompt": "p", "gate": "human"},
            {
                "id": "b",
                "subagent": "researcher",
                "depends_on": ["a"],
                "prompt": "{{steps.a.output}}",
                "gate": "human",
            },
        ],
    }
    res = asyncio.run(
        execute_workflow(recipe, {}, run_step=run_step, gate_check=gate_check, pause_fn=pause_fn)
    )
    assert res["paused_step"] == "b"  # paused at the *second* gate, in turn
    assert spawned == ["a"]  # a ran (approved); b never spawned
    assert res["steps"] == {"a": "<a>"}  # a's output carried into the paused envelope
    assert paused_with == [("b", {"a": "<a>"})]  # pause_fn saw the completed outputs


def test_execute_without_gate_check_ignores_gate_field():
    """gate_check=None ⇒ the pre-gate code path: a recipe carrying gate:human still
    runs straight through (this is the guarantee ungated workflows rely on)."""

    async def run_step(subagent, prompt, sid):
        return f"<{sid}-out>"

    res = asyncio.run(execute_workflow(GATED, {"topic": "ai"}, run_step=run_step))
    assert "paused" not in res
    assert res["output"] == "<analyze-out>"
    assert res["failed"] == []


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


def test_register_sees_plugin_workflow_dirs_added_after_load(tmp_path, monkeypatch):
    """Installed-plugin recipe dirs are only complete on STATE.plugin_workflow_dirs
    AFTER the full plugin load — the workflows plugin registers earlier (in-tree
    plugins load first), so an eager scan would permanently miss them (the ADR 0027
    bundle promise). The registry must resolve lazily: a dir that lands on STATE
    after register() shows up on the next access, without a reload."""
    from types import SimpleNamespace

    import plugins.workflows as wf
    import runtime.state as rs

    writable = tmp_path / "writable"
    monkeypatch.setattr(wf.sdk, "config", lambda: SimpleNamespace(workflow_dir=str(writable)))
    monkeypatch.setattr(wf.sdk, "subagent_types", lambda: {"researcher"})
    monkeypatch.setattr(rs.STATE, "workflow_registry", None, raising=False)
    monkeypatch.setattr(rs.STATE, "workflow_run", None, raising=False)
    monkeypatch.setattr(rs.STATE, "plugin_workflow_dirs", [], raising=False)

    class _Reg:
        workflow_dirs: list = []

        def register_tools(self, tools):
            pass

        def register_workflow_dir(self, d):
            pass

        def register_router(self, router, prefix=None):
            pass

    wf.register(_Reg())
    baseline = {s["name"] for s in rs.STATE.workflow_registry.list()}
    assert "late" not in baseline

    # A git-installed plugin's workflows/ dir lands on STATE after this plugin loaded.
    late_dir = tmp_path / "late-plugin" / "workflows"
    late_dir.mkdir(parents=True)
    (late_dir / "late.yaml").write_text(
        "name: late\ndescription: from an installed plugin\nsteps:\n"
        "  - id: a\n    subagent: researcher\n    prompt: p\n",
        encoding="utf-8",
    )
    rs.STATE.plugin_workflow_dirs = [str(late_dir)]

    names = {s["name"] for s in rs.STATE.workflow_registry.list()}
    assert "late" in names  # no reload, no re-register — the proxy rescanned


# --- _execute + run-state wiring for gated runs (F2) --------------------------


class _GatedReg:
    """Minimal registry that only knows the GATED recipe (for _execute wiring tests)."""

    def get(self, name):
        return GATED if name == GATED["name"] else None


def _patch_sdk(monkeypatch, run_subagent, workflow_dir=""):
    from types import SimpleNamespace

    import plugins.workflows as wf

    monkeypatch.setattr(wf.sdk, "subagent_types", lambda: {"researcher"})
    monkeypatch.setattr(wf.sdk, "run_subagent", run_subagent)
    monkeypatch.setattr(
        wf.sdk, "config", lambda: SimpleNamespace(subagent_max_concurrency=2, workflow_dir=workflow_dir)
    )


def test_execute_pauses_at_gated_step_and_persists_paused_state(tmp_path, monkeypatch):
    import plugins.workflows as wf
    from plugins.workflows.run_state import STATUS_PAUSED, WorkflowRunStore

    spawned = []

    async def run_subagent(subagent_type, prompt, description=""):
        sid = description.rsplit(":", 1)[-1]
        spawned.append(sid)
        return f"<{sid}-out>"

    _patch_sdk(monkeypatch, run_subagent)
    store = WorkflowRunStore(tmp_path)
    result = asyncio.run(wf._execute(_GatedReg(), "gated", {"topic": "ai"}, run_store=store))

    # Structured envelope — what the /workflows/{name}/run API returns verbatim.
    assert result["paused"] is True
    assert result["paused_step"] == "analyze"
    assert result["run_id"] == store.run_id
    assert result["steps"] == {"gather": "<gather-out>"}
    # Human-readable notice — the run_workflow tool return / chat /<recipe> reply.
    assert result["output"] == wf._paused_message(result)
    assert store.run_id in result["output"] and "analyze" in result["output"]
    # No wasted work: the gated step's subagent was never spawned.
    assert spawned == ["gather"]
    # Durable paused record on disk: status, pending_step, and all prior outputs.
    state = store.load(result["run_id"])
    assert state["status"] == STATUS_PAUSED
    assert state["pending_step"] == "analyze"
    assert state["step_outputs"] == {"gather": "<gather-out>"}
    assert state["inputs"] == {"topic": "ai"}


def test_execute_ungated_recipe_still_finishes_done(tmp_path, monkeypatch):
    """The gate wiring is inert for a recipe with no gated step — it runs to completion."""
    import plugins.workflows as wf
    from plugins.workflows.run_state import STATUS_DONE, WorkflowRunStore

    async def run_subagent(subagent_type, prompt, description=""):
        return f"<{description.rsplit(':', 1)[-1]}-out>"

    ungated = {**GATED, "name": "ungated", "steps": [dict(s) for s in GATED["steps"]]}
    ungated["steps"][1].pop("gate")  # drop the gate from `analyze`

    class _Reg:
        def get(self, name):
            return ungated if name == "ungated" else None

    _patch_sdk(monkeypatch, run_subagent)
    store = WorkflowRunStore(tmp_path)
    result = asyncio.run(wf._execute(_Reg(), "ungated", {"topic": "ai"}, run_store=store))
    assert "paused" not in result
    assert result["output"] == "<analyze-out>"
    assert store.load(result["run_id"])["status"] == STATUS_DONE


def test_run_store_pause_persists_paused_status(tmp_path):
    from plugins.workflows.run_state import STATUS_PAUSED, WorkflowRunStore

    store = WorkflowRunStore(tmp_path)
    run_id = store.start("gated", {"topic": "ai"})
    store.step_done("gather", "found")
    returned = store.pause("analyze", {"gather": "found"})
    assert returned == run_id
    state = store.load(run_id)
    assert state["status"] == STATUS_PAUSED
    assert state["pending_step"] == "analyze"
    assert state["step_outputs"] == {"gather": "found"}
    # pause() before start() is a no-op returning None.
    assert WorkflowRunStore(tmp_path / "other").pause("x") is None


def test_paused_message_matches_expected_shape():
    import plugins.workflows as wf

    msg = wf._paused_message({"paused_step": "analyze", "run_id": "abc123"})
    assert msg == (
        "⚠️ Workflow paused at step 'analyze' for operator approval. "
        "Run id: abc123. Resume from the Workflows panel."
    )
