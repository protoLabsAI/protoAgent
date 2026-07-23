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
    assert any(
        "unsupported gate" in e
        for e in validate_recipe(
            {**bad, "steps": [{"id": "a", "subagent": "researcher", "prompt": "p", "gate": True}]},
            known_subagents={"researcher"},
        )
    )


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
    assert res["paused"] is True and res["paused_step"] == "analyze" and res["run_id"] == "run-abc"
    assert res["steps"] == {"gather": "<gather-out>"}
    assert "gather" in res["timings"]  # the step that ran before the gate is timed


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
    assert res["paused"] is True and res["paused_step"] == "first"
    assert res["run_id"] == "rid" and res["steps"] == {} and res["timings"] == {}


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
    res = asyncio.run(execute_workflow(recipe, {}, run_step=run_step, gate_check=gate_check, pause_fn=pause_fn))
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
    # Human-readable status block — the run_workflow tool return / chat /<recipe> reply.
    assert result["output"] == wf._paused_message("gated", result)
    assert store.run_id in result["output"] and "analyze" in result["output"]
    # Actionable without the console: recipe, prior outputs, and resume paths included.
    assert "gated" in result["output"]
    assert "<gather-out>" in result["output"]
    assert "Pending Gates" in result["output"]
    assert f"POST /api/plugins/workflows/runs/{store.run_id}/resume" in result["output"]
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
    """The pause notice is a full status block (F4): recipe, paused step, run id,
    prior step outputs as decision context, and both resume paths — not a one-liner."""
    import plugins.workflows as wf

    msg = wf._paused_message(
        "gated",
        {"paused_step": "analyze", "run_id": "abc123", "steps": {"gather": "found things"}},
    )
    assert msg == (
        "⏸️ Workflow 'gated' paused — step 'analyze' needs operator approval.\n"
        "\n"
        "- Recipe: gated\n"
        "- Paused step: analyze\n"
        "- Run id: abc123\n"
        "\n"
        "Completed steps so far:\n"
        "- gather: found things\n"
        "\n"
        "Resume from the console's Workflows → Pending Gates panel, or via "
        "POST /api/plugins/workflows/runs/abc123/resume (action: approve | edit | reject)."
    )


def test_paused_message_truncates_long_outputs_and_skips_empty_steps():
    import plugins.workflows as wf

    long_out = "word " * 200  # collapses to 999 chars — over the preview cap
    msg = wf._paused_message("gated", {"paused_step": "s2", "run_id": "r1", "steps": {"s1": long_out}})
    line = next(ln for ln in msg.splitlines() if ln.startswith("- s1: "))
    assert line.endswith("…") and len(line) < 450  # capped preview, whitespace collapsed
    # A first-step gate has no prior outputs — the block omits the section entirely.
    first = wf._paused_message("gated", {"paused_step": "s1", "run_id": "r1", "steps": {}})
    assert "Completed steps so far" not in first
    assert "- Run id: r1" in first


# --- Tool-level UX for gated workflows (F4) -----------------------------------


class _CapturingReg:
    """Plugin registry double that captures the registered tools by name."""

    def __init__(self):
        self.tools = {}

    def register_tools(self, tools):
        self.tools.update({t.name: t for t in tools})

    def register_workflow_dir(self, d):
        pass

    def register_router(self, router, prefix=None):
        pass


def _register_plugin(tmp_path, monkeypatch, run_subagent):
    """Register the workflows plugin against a tmp writable dir + stubbed SDK and
    return its tools ({name: tool}) — the run_workflow/save_workflow surface."""
    import plugins.workflows as wf
    import runtime.state as rs

    _patch_sdk(monkeypatch, run_subagent, workflow_dir=str(tmp_path / "writable"))
    monkeypatch.setattr(rs.STATE, "workflow_registry", None, raising=False)
    monkeypatch.setattr(rs.STATE, "workflow_run", None, raising=False)
    monkeypatch.setattr(rs.STATE, "plugin_workflow_dirs", [], raising=False)
    reg = _CapturingReg()
    wf.register(reg)
    return reg.tools


GATED_STEPS = [
    {"id": "gather", "subagent": "researcher", "prompt": "read {{inputs.file}}"},
    {
        "id": "analyze",
        "subagent": "researcher",
        "depends_on": ["gather"],
        "prompt": "analyze:\n{{steps.gather.output}}",
        "gate": "human",
    },
]


async def test_run_workflow_tool_pause_return_is_actionable_status_block(tmp_path, monkeypatch):
    """On a gate the tool returns the full status block — recipe, paused step, run id,
    prior step outputs, and where to resume — so the operator can act from chat
    without opening the console panel."""
    from plugins.workflows.run_state import WorkflowRunStore

    spawned = []

    async def run_subagent(subagent_type, prompt, description=""):
        sid = description.rsplit(":", 1)[-1]
        spawned.append(sid)
        return f"<{sid}-out>"

    tools = _register_plugin(tmp_path, monkeypatch, run_subagent)
    saved = await tools["save_workflow"].ainvoke(
        {
            "name": "careercoach-resume",
            "description": "review a resume with a human gate before the writeup",
            "steps": GATED_STEPS,
            "inputs": [{"name": "file", "required": True}],
        }
    )
    assert "Saved workflow 'careercoach-resume'" in saved

    out = await tools["run_workflow"].ainvoke({"name": "careercoach-resume", "inputs": {"file": "cv.pdf"}})
    (run_id,) = WorkflowRunStore(tmp_path / "writable" / ".runs").list_runs()
    # Names the recipe and the paused step, carries the run id.
    assert "'careercoach-resume'" in out
    assert "- Paused step: analyze" in out
    assert f"- Run id: {run_id}" in out
    # Prior step outputs are included as decision context.
    assert "- gather: <gather-out>" in out
    # Says where to resume — panel AND API.
    assert "Pending Gates" in out
    assert f"POST /api/plugins/workflows/runs/{run_id}/resume" in out
    # The gated step's subagent was never spawned.
    assert spawned == ["gather"]


async def test_save_workflow_round_trips_human_gate(tmp_path, monkeypatch):
    """save_workflow recognizes `gate: human` on a step dict: the confirmation names
    the gated step, and the saved recipe carries the gate — durably, on disk."""
    import runtime.state as rs

    async def run_subagent(subagent_type, prompt, description=""):
        return "unused"

    tools = _register_plugin(tmp_path, monkeypatch, run_subagent)
    msg = await tools["save_workflow"].ainvoke(
        {
            "name": "gated-save",
            "description": "gate round-trip",
            "steps": GATED_STEPS,
            "inputs": [{"name": "file", "required": True}],
        }
    )
    assert "Gated step(s) analyze will pause for operator approval when run." in msg

    recipe = rs.STATE.workflow_registry.get("gated-save")
    steps = {s["id"]: s for s in recipe["steps"]}
    assert steps["analyze"]["gate"] == "human"  # preserved on the right step
    assert "gate" not in steps["gather"]  # and not invented on ungated ones
    # The YAML on disk carries it too — the round-trip survives a reload.
    assert "gate: human" in (tmp_path / "writable" / "gated-save.yaml").read_text(encoding="utf-8")


async def test_save_workflow_rejects_unsupported_gate(tmp_path, monkeypatch):
    async def run_subagent(subagent_type, prompt, description=""):
        return "unused"

    tools = _register_plugin(tmp_path, monkeypatch, run_subagent)
    msg = await tools["save_workflow"].ainvoke(
        {
            "name": "bad-gate",
            "description": "x",
            "steps": [{"id": "a", "subagent": "researcher", "prompt": "p", "gate": "robot"}],
        }
    )
    assert "Cannot save" in msg and "unsupported gate" in msg


# ── fan-out width + timings ──────────────────────────────────────────────────


def _fanout_recipe(n: int, width=None) -> dict:
    r = {
        "name": "fanout",
        "steps": [{"id": f"s{i}", "subagent": "x", "prompt": "go"} for i in range(n)]
        + [{"id": "join", "subagent": "x", "prompt": "j", "depends_on": [f"s{i}" for i in range(n)]}],
    }
    if width is not None:
        r["max_concurrency"] = width
    return r


async def _peak_concurrency(recipe, caller_width):
    """Run the recipe, returning the max number of steps in flight at once."""
    live = 0
    peak = 0

    async def run_step(_sub, _prompt, sid):
        nonlocal live, peak
        live += 1
        peak = max(peak, live)
        await asyncio.sleep(0.01)
        live -= 1
        return sid

    await execute_workflow(recipe, {}, run_step=run_step, max_concurrency=caller_width)
    return peak


async def test_a_stage_wider_than_the_callers_cap_is_serialized_without_a_declaration():
    # The bug: a five-step parallel stage under a cap of four runs 4+1 and pays twice
    # the slowest step. This is what a review panel was silently doing in production.
    assert await _peak_concurrency(_fanout_recipe(5), caller_width=4) == 4


async def test_a_declared_width_lets_the_whole_stage_run_at_once():
    assert await _peak_concurrency(_fanout_recipe(5, width=5), caller_width=4) == 5


async def test_a_declared_width_can_also_narrow_a_stage():
    # Not only an escape hatch upward — a recipe hitting a rate-limited tool can ask
    # for less than the caller would allow.
    assert await _peak_concurrency(_fanout_recipe(5, width=2), caller_width=8) == 2


async def test_an_absurd_width_is_a_validation_error_not_a_stampede():
    from plugins.workflows.engine import MAX_FANOUT, validate_recipe

    assert validate_recipe(_fanout_recipe(2, width=MAX_FANOUT + 1)) != []
    assert validate_recipe(_fanout_recipe(2, width=0)) != []
    assert validate_recipe(_fanout_recipe(2, width="lots")) != []
    assert validate_recipe(_fanout_recipe(2, width=True)) != []  # bools are not widths
    assert validate_recipe(_fanout_recipe(2, width=MAX_FANOUT)) == []
    assert validate_recipe(_fanout_recipe(2)) == []  # absent stays valid


async def test_timings_are_reported_per_step_and_exclude_queueing():
    # A step held behind the semaphore must not be billed for the wait — otherwise a
    # serialized wave reads as a slow step, hiding the very problem width fixes.
    async def run_step(_sub, _prompt, sid):
        await asyncio.sleep(0.05 if sid == "s0" else 0.01)
        return sid

    res = await execute_workflow(_fanout_recipe(3), {}, run_step=run_step, max_concurrency=1)
    assert set(res["timings"]) == {"s0", "s1", "s2", "join"}
    assert res["timings"]["s0"] >= 0.05
    assert res["timings"]["s2"] < 0.04  # ran last, but is billed only for its own work


async def test_a_failed_step_is_still_timed():
    async def run_step(_sub, _prompt, sid):
        raise RuntimeError("boom")

    res = await execute_workflow(_fanout_recipe(1), {}, run_step=run_step, max_concurrency=2)
    assert res["failed"]
    assert "s0" in res["timings"]  # you especially want to know how long a failure took
