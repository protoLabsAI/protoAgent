"""Workflows plugin — declarative, multi-step subagent workflows (ADR 0002).

Extracted from core to an **opt-in plugin** (lean core): the engine/registry live here,
and the engine taps core **only via the plugin SDK** (`graph.sdk.run_subagent` +
`subagent_types`) — the first real consumer of the consumption SDK, never importing
`graph.agent` internals. `enabled: false` in the manifest → off by default.

Contributes:
  • tools  `run_workflow`, `save_workflow` (the agent runs/saves recipes)
  • router `/api/plugins/workflows/{list, {name}/run, save, {name}}` (the console Studio surface)
  • recipe dir (its bundled recipes, also exposed to the shared registry per ADR 0027)
  • run state — every execution persists a durable per-run audit record (`run_state.py`)
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Awaitable, Callable

from langchain_core.tools import tool

from graph import sdk
from plugins.workflows.engine import execute_workflow, render_template, resolve_inputs, validate_recipe
from plugins.workflows.registry import WorkflowRegistry
from plugins.workflows.run_state import STATUS_DONE, STATUS_FAILED, STATUS_PAUSED, WorkflowRunStore

_RECIPES = Path(__file__).parent / "recipes"


def _paused_message(result: dict) -> str:
    """Operator-facing pause notice — the ``run_workflow`` tool return and the chat
    ``/<recipe>`` reply. The console reads the structured ``paused``/``run_id`` fields."""
    return (
        f"⚠️ Workflow paused at step {result['paused_step']!r} for operator approval. "
        f"Run id: {result['run_id']}. Resume from the Workflows panel."
    )


def _writable_dir() -> Path:
    """The writable workflow dir — saved recipes AND run state (``.runs/``) live here.
    ``workflow_dir`` config is used verbatim when an operator overrides it; the legacy
    ``/sandbox`` default maps to the per-instance ``instance_root/workflows`` store."""
    from infra.paths import instance_paths

    cfg = sdk.config()
    configured = getattr(cfg, "workflow_dir", "") or ""
    if configured and not str(configured).startswith("/sandbox"):
        writable = Path(configured).expanduser()
    else:
        writable = instance_paths().store("workflows")
    writable.mkdir(parents=True, exist_ok=True)
    return writable


def _build_registry(extra_dirs: list[str] | None) -> WorkflowRegistry:
    """Bundled recipes + other enabled plugins' recipe dirs (ADR 0027) + a writable dir
    (user/agent-saved recipes win on a name clash)."""
    dirs: list[str] = [str(_RECIPES)]
    for d in extra_dirs or []:
        if Path(d).is_dir():
            dirs.append(str(d))
    writable = _writable_dir()
    dirs.append(str(writable))
    return WorkflowRegistry(dirs, writable_dir=str(writable))


async def _execute(reg: WorkflowRegistry, name: str, inputs: dict, on_step=None, run_store=None) -> dict:
    """Validate → resolve → run a recipe over subagents (each step via the SDK). Raises
    ValueError on unknown/invalid recipe or missing inputs.

    Every execution gets a UUID run_id and a durable audit trail: a ``WorkflowRunStore``
    (default: ``{writable_dir}/.runs/{run_id}.json``) is written on start, after each
    completed step, and on finish — the run flow itself is unchanged."""
    recipe = reg.get(name)
    if recipe is None:
        raise ValueError(f"no workflow named {name!r}")
    errs = validate_recipe(recipe, known_subagents=sdk.subagent_types())
    if errs:
        raise ValueError("invalid workflow: " + "; ".join(errs))
    resolved, missing = resolve_inputs(recipe, inputs or {})
    if missing:
        raise ValueError(f"missing required input(s): {', '.join(missing)}")

    if run_store is None:
        run_store = WorkflowRunStore(_writable_dir() / ".runs")
    run_id = run_store.start(name, resolved)

    async def _run_step(subagent_type: str, prompt: str, step_id: str) -> str:
        if on_step:
            await _safe(on_step, {"phase": "start", "step_id": step_id, "subagent": subagent_type})
        out = await sdk.run_subagent(subagent_type, prompt, description=f"workflow {name}:{step_id}")
        run_store.step_done(step_id, out)
        if on_step:
            await _safe(on_step, {"phase": "end", "step_id": step_id, "subagent": subagent_type, "output": out})
        return out

    def _gate_check(step: dict) -> str | None:
        # `gate: human` parks the run for operator approval before the step is dispatched.
        # validate_recipe guarantees `human` is the only accepted value, so anything else
        # (absent gate included) runs normally.
        return "pause" if step.get("gate") == "human" else None

    def _pause(step_id: str, completed: dict) -> str | None:
        return run_store.pause(step_id, completed)

    try:
        result = await execute_workflow(
            recipe,
            resolved,
            run_step=_run_step,
            max_concurrency=getattr(sdk.config(), "subagent_max_concurrency", 3),
            gate_check=_gate_check,
            pause_fn=_pause,
        )
    except Exception:
        run_store.finish(STATUS_FAILED)
        raise
    if result.get("paused"):  # parked at a `gate: human` step — durable + resumable, not terminal
        result.setdefault("run_id", run_id)
        result["output"] = _paused_message(result)
        return result
    # Failed steps never reach _run_step's step_done (the engine records their error
    # text inline) — mirror that error text into the run record.
    for sid in result["failed"]:
        run_store.step_done(sid, result["steps"][sid])
    run_store.finish(STATUS_FAILED if result["failed"] else STATUS_DONE)
    result["run_id"] = run_id
    return result


async def _safe(cb: Callable[[dict], Awaitable[None]], event: dict) -> None:
    try:
        await cb(event)
    except Exception:  # noqa: BLE001 — progress is best-effort, never fatal
        pass


def _rendered_gate_prompt(recipe: dict, state: dict) -> str:
    """The paused step's prompt, templated with the run's inputs + prior outputs — what
    the operator actually approves (never raw ``{{...}}`` syntax)."""
    pending = state.get("pending_step")
    step = next((s for s in recipe.get("steps", []) if s.get("id") == pending), None) if recipe else None
    if step is None:
        return ""
    return render_template(step.get("prompt", ""), state.get("inputs") or {}, state.get("step_outputs") or {})


def _paused_run_view(reg: WorkflowRegistry, state: dict) -> dict:
    """One paused run as the console's Pending Gates card consumes it: identity, the
    step it's parked on, that step's RENDERED prompt, the prior outputs, timestamps."""
    return {
        "run_id": state.get("run_id"),
        "recipe_name": state.get("recipe_name"),
        "paused_step": state.get("pending_step"),
        "prompt": _rendered_gate_prompt(reg.get(state.get("recipe_name")), state),
        "step_outputs": state.get("step_outputs") or {},
        "inputs": state.get("inputs") or {},
        "created_at": state.get("created_at"),
        "updated_at": state.get("updated_at"),
    }


def _list_paused_runs(reg: WorkflowRegistry, run_store: WorkflowRunStore | None = None) -> list[dict]:
    """Every paused run (the Pending Gates queue), rendered for the console. Empty when
    none are parked."""
    if run_store is None:
        run_store = WorkflowRunStore(_writable_dir() / ".runs")
    return [_paused_run_view(reg, state) for state in run_store.paused()]


async def _resume(
    reg: WorkflowRegistry,
    run_id: str,
    action: str,
    edits: dict | None = None,
    run_store: WorkflowRunStore | None = None,
) -> dict:
    """Continue a paused run from its parked step. ``action``:

    * ``approve`` — run the gated step with its original (templated) prompt.
    * ``edit``    — run the gated step with ``edits["prompt"]`` verbatim; downstream
      steps see the edited step's output.
    * ``reject``  — mark the gated step failed (``rejected by operator``); the DAG
      continues and dependents inherit the error (inline-failure semantics).

    The stored state (recipe, inputs, completed outputs) is re-fed to
    ``execute_workflow`` with the done steps seeded, so nothing already-run re-runs.
    The run flips to ``running`` and then to ``done``/``failed`` (or re-``paused`` if a
    *downstream* gate is hit). Raises ``ValueError`` on an unknown/non-paused run."""
    if action not in ("approve", "edit", "reject"):
        raise ValueError(f"unknown resume action {action!r} (approve | edit | reject)")
    # Validate EVERYTHING up front — before the registry is consulted and long before
    # the run flips to `running` on disk. Failing later would orphan the run: gone
    # from the pending list, stuck `running`, unresumable (QA panel blocker).
    if action == "edit" and not str((edits or {}).get("prompt", "")).strip():
        raise ValueError("edit action requires a non-empty edits.prompt")
    if run_store is None:
        run_store = WorkflowRunStore(_writable_dir() / ".runs")

    state = run_store.load(run_id)
    if state is None:
        raise ValueError(f"no run {run_id!r}")
    if state.get("status") != STATUS_PAUSED:
        raise ValueError(f"run {run_id!r} is not paused (status: {state.get('status')})")
    pending_step = state.get("pending_step")
    if not pending_step:
        raise ValueError(f"run {run_id!r} has no pending step to resume")

    name = state["recipe_name"]
    recipe = reg.get(name)
    if recipe is None:
        raise ValueError(f"no workflow named {name!r}")
    inputs = dict(state.get("inputs") or {})
    completed = dict(state.get("step_outputs") or {})

    # Re-attach the store to this run and flip it back to `running` before dispatching.
    run_store.resume(run_id)

    async def _run_step(subagent_type: str, prompt: str, step_id: str) -> str:
        out = await sdk.run_subagent(subagent_type, prompt, description=f"workflow {name}:{step_id}")
        run_store.step_done(step_id, out)
        return out

    def _gate_check(step: dict) -> str | None:
        return "pause" if step.get("gate") == "human" else None

    kwargs: dict[str, Any] = {
        "run_step": _run_step,
        "max_concurrency": getattr(sdk.config(), "subagent_max_concurrency", 3),
        "gate_check": _gate_check,
        "pause_fn": lambda step_id, done: run_store.pause(step_id, done),
        "seed_outputs": completed,
        "skip_gate": {pending_step},  # the operator already decided this gate's fate
    }
    if action == "reject":
        kwargs["prefailed"] = {pending_step: "rejected by operator"}
    elif action == "edit":
        edited = (edits or {}).get("prompt")
        if edited is None:
            raise ValueError("edit resume requires edits.prompt")
        kwargs["prompt_overrides"] = {pending_step: edited}

    try:
        result = await execute_workflow(recipe, inputs, **kwargs)
    except Exception:
        run_store.finish(STATUS_FAILED)
        raise
    if result.get("paused"):  # a DOWNSTREAM gate — durable + resumable again, not terminal
        result.setdefault("run_id", run_id)
        result["output"] = _paused_message(result)
        return result
    # Failed steps (inline errors + a reject) never hit _run_step's step_done — mirror
    # their error text into the record, matching _execute's finish path.
    for sid in result["failed"]:
        run_store.step_done(sid, result["steps"][sid])
    run_store.finish(STATUS_FAILED if result["failed"] else STATUS_DONE)
    result["run_id"] = run_id
    return result


def register(registry: Any) -> None:
    # Other plugins' recipe dirs are NOT knowable here: every plugin gets its own
    # PluginRegistry, and this in-tree plugin registers before the instance-installed
    # ones, so the accumulated dir list (STATE.plugin_workflow_dirs) is only complete
    # AFTER the full load — an eager scan would permanently miss every git-installed
    # plugin's workflows/ dir (the ADR 0027 bundle promise). Resolve lazily instead:
    # every access goes through _reg(), which rebuilds the WorkflowRegistry whenever
    # the plugin-dir set changed (first use after boot, hot install, config reload).
    # Rescanning a handful of YAML files is cheap; staleness here is silent data loss.
    from runtime.state import STATE

    _cache: dict[str, Any] = {"dirs": None, "reg": None}

    def _reg() -> WorkflowRegistry:
        dirs = tuple(str(d) for d in (getattr(STATE, "plugin_workflow_dirs", None) or ()))
        if _cache["reg"] is None or dirs != _cache["dirs"]:
            _cache["dirs"] = dirs
            _cache["reg"] = _build_registry(list(dirs))
        return _cache["reg"]

    class _LiveRegistry:
        """What STATE.workflow_registry publishes — a thin proxy so consumers that
        grabbed it once (chat slash-command, console) always see the current scan."""

        def __getattr__(self, name: str) -> Any:
            return getattr(_reg(), name)

    # Publish the registry + a runner onto runtime state so core surfaces that predate
    # the plugin (the chat `/<recipe>` slash-command) can use workflows WITHOUT importing
    # this plugin — both are None when the plugin is disabled, which gates those paths.
    async def _run(name: str, inputs: dict | None = None, on_step=None) -> dict:
        return await _execute(_reg(), name, inputs or {}, on_step)

    STATE.workflow_registry = _LiveRegistry()
    STATE.workflow_run = _run

    @tool
    async def run_workflow(name: str = "", inputs: dict | None = None) -> str:
        """Run a saved multi-step workflow recipe over subagents.

        Workflows chain subagent steps (some in parallel), threading each step's output
        into the next — for repeatable jobs like research→synthesize→write. Pass an empty
        ``name`` to list the available workflows and their inputs.

        Args:
            name: The workflow name (empty lists them).
            inputs: Mapping of the workflow's declared inputs to values.
        """
        if not name.strip():
            summaries = _reg().list()
            if not summaries:
                return "No workflows are available."
            lines = ["Available workflows:"]
            for s in summaries:
                req = [i["name"] for i in s["inputs"] if i["required"]]
                lines.append(f"- {s['name']}: {s['description']} (inputs: {', '.join(req) or 'none required'})")
            return "\n".join(lines)
        try:
            result = await _execute(_reg(), name, inputs or {})
        except ValueError as exc:
            return f"Workflow {name!r}: {exc}"
        if result.get("paused"):  # gated step reached — tell the operator how to resume
            return _paused_message(result)
        return result["output"]

    @tool
    async def save_workflow(
        name: str,
        description: str,
        steps: list[dict],
        inputs: list[dict] | None = None,
        output: str = "",
    ) -> str:
        """Save a reusable multi-step workflow so it can be re-run with run_workflow —
        capture a multi-step subagent process you just worked out. Overwrites a workflow
        of the same name.

        Args:
            name: Unique slug.
            description: One-line summary.
            steps: Ordered step objects: ``id``, ``subagent`` (a configured subagent),
                ``prompt`` (may reference {{inputs.x}} / {{steps.<id>.output}}), optional
                ``depends_on`` (earlier step ids; independent steps run in parallel), and
                optional ``gate`` (``human`` pauses the run for operator approval before
                this step runs).
            inputs: Optional [{name, required?, default?}] (referenced as {{inputs.name}}).
            output: Optional final-output template (default = last step's output).
        """
        recipe: dict = {"name": name, "description": description, "version": 1, "steps": steps}
        if inputs:
            recipe["inputs"] = inputs
        if output:
            recipe["output"] = output
        errs = validate_recipe(recipe, known_subagents=sdk.subagent_types())
        if errs:
            return "Cannot save — the workflow is invalid: " + "; ".join(errs)
        try:
            path = _reg().save(recipe)
        except Exception as exc:  # noqa: BLE001 — readable tool error
            return f"Error saving workflow: {exc}"
        return f"Saved workflow {name!r} ({len(steps)} step(s)) to {path}. Run it with run_workflow({name!r}, ...)."

    registry.register_tools([run_workflow, save_workflow])
    registry.register_workflow_dir(str(_RECIPES))

    # Operator API — the console Studio surface calls these.
    from fastapi import APIRouter, Body, HTTPException

    router = APIRouter()

    @router.get("/list")
    async def _list() -> dict:
        return {"workflows": _reg().list()}

    @router.post("/{name}/run")
    async def _run_route(name: str, body: dict = Body(default={})) -> dict:
        try:
            return await _execute(_reg(), name, (body or {}).get("inputs") or {})
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @router.post("/save")
    async def _save(body: dict = Body(...)) -> dict:
        errs = validate_recipe(body, known_subagents=sdk.subagent_types())
        if errs:
            raise HTTPException(status_code=400, detail="invalid recipe: " + "; ".join(errs))
        path = _reg().save(body)
        return {"saved": True, "name": body.get("name"), "path": path}

    @router.get("/runs")
    async def _runs() -> dict:
        # The Pending Gates queue — only PAUSED runs, each with its parked step's
        # rendered prompt + prior outputs. Empty list when nothing is gated.
        return {"runs": _list_paused_runs(_reg())}

    @router.post("/runs/{run_id}/resume")
    async def _resume_route(run_id: str, body: dict = Body(default={})) -> dict:
        body = body or {}
        try:
            return await _resume(_reg(), run_id, body.get("action") or "approve", body.get("edits") or {})
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @router.delete("/{name}")
    async def _delete(name: str) -> dict:
        return {"deleted": _reg().delete(name)}

    registry.register_router(router, prefix="/api/plugins/workflows")
