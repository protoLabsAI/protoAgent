"""Workflows plugin — declarative, multi-step subagent workflows (ADR 0002).

Extracted from core to an **opt-in plugin** (lean core): the engine/registry live here,
and the engine taps core **only via the plugin SDK** (`graph.sdk.run_subagent` +
`subagent_types`) — the first real consumer of the consumption SDK, never importing
`graph.agent` internals. `enabled: false` in the manifest → off by default.

Contributes:
  • tools  `run_workflow`, `save_workflow` (the agent runs/saves recipes)
  • router `/api/plugins/workflows/{list, {name}/run, {name}/start, save, validate,
    runs, runs/all, runs/{id}, runs/{id}/resume, {name}}` (the console Studio surface)
  • recipe dir (its bundled recipes, also exposed to the shared registry per ADR 0027)
  • run state — every execution persists a durable per-run audit record (`run_state.py`)

Two run shapes serve two callers. The agent tool and the legacy `POST /{name}/run`
are **synchronous** — the caller wants the final output in the reply. The Studio
uses `POST /{name}/start`: inputs are validated up front (a bad request 400s, it
never mints a failed run), the DAG executes detached, and the console polls
`GET /runs/{run_id}` — whose record now carries the step graph + per-step lifecycle
(`step_started`/`step_done`) — to render a live timeline.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any, Awaitable, Callable

from langchain_core.tools import tool

from graph import sdk
from plugins.workflows.engine import execute_workflow, render_template, resolve_inputs, validate_recipe
from plugins.workflows.registry import WorkflowRegistry
from plugins.workflows.run_state import STATUS_DONE, STATUS_FAILED, STATUS_PAUSED, WorkflowRunStore

log = logging.getLogger("protoagent.plugins.workflows")

_RECIPES = Path(__file__).parent / "recipes"

_PAUSE_PREVIEW = 400  # chars of each prior step's output shown in the pause notice

# Terminal-run retention (`.runs/` pruning); the manifest's `max_runs` overrides in register().
_MAX_RUNS = 200

# Detached Studio runs — referenced so the loop can't GC an in-flight DAG.
_BG_TASKS: set[asyncio.Task] = set()


def _spawn(coro: Awaitable) -> asyncio.Task:
    task = asyncio.get_running_loop().create_task(coro)
    _BG_TASKS.add(task)
    task.add_done_callback(_BG_TASKS.discard)
    return task


def _paused_message(name: str, result: dict) -> str:
    """Operator-facing pause status block — the ``run_workflow`` tool return and the
    chat ``/<recipe>`` reply (the console reads the structured ``paused``/``run_id``
    fields instead). Self-sufficient by design: recipe, parked step, run id, the
    prior steps' outputs (the operator's decision context), and both resume paths —
    everything needed to act without opening the panel."""
    run_id = result.get("run_id")
    lines = [
        f"⏸️ Workflow {name!r} paused — step {result['paused_step']!r} needs operator approval.",
        "",
        f"- Recipe: {name}",
        f"- Paused step: {result['paused_step']}",
        f"- Run id: {run_id}",
    ]
    prior = result.get("steps") or {}
    if prior:
        lines += ["", "Completed steps so far:"]
        for sid, out in prior.items():
            preview = " ".join(str(out).split())
            if len(preview) > _PAUSE_PREVIEW:
                preview = preview[:_PAUSE_PREVIEW] + "…"
            lines.append(f"- {sid}: {preview}")
    lines += [
        "",
        "Resume from the console's Workflows → Pending Gates panel, or via "
        f"POST /api/plugins/workflows/runs/{run_id}/resume (action: approve | edit | reject).",
    ]
    return "\n".join(lines)


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


def _prepare(reg: WorkflowRegistry, name: str, inputs: dict) -> tuple[dict, dict]:
    """Validate the recipe + resolve inputs — everything that should fail the CALLER
    (a 400 / readable tool error), long before a run record exists. Raises ValueError."""
    recipe = reg.get(name)
    if recipe is None:
        raise ValueError(f"no workflow named {name!r}")
    errs = validate_recipe(recipe, known_subagents=sdk.subagent_types())
    if errs:
        raise ValueError("invalid workflow: " + "; ".join(errs))
    resolved, missing = resolve_inputs(recipe, inputs or {})
    if missing:
        raise ValueError(f"missing required input(s): {', '.join(missing)}")
    return recipe, resolved


async def _run_prepared(
    recipe: dict,
    name: str,
    resolved: dict,
    run_store: WorkflowRunStore,
    run_id: str,
    on_step=None,
) -> dict:
    """Execute an already-validated recipe against its live run record: the store is
    updated at each step dispatch/completion (the Studio's polling target) and closed
    out with the final envelope on finish/pause."""

    async def _run_step(subagent_type: str, prompt: str, step_id: str) -> str:
        run_store.step_started(step_id)
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
            # Caller default only — a recipe declaring its own fan-out width wins inside
            # the engine (a 5-step parallel stage must not be serialized by a cap of 4).
            max_concurrency=getattr(sdk.config(), "subagent_max_concurrency", 3),
            gate_check=_gate_check,
            pause_fn=_pause,
        )
    except Exception:
        run_store.finish(STATUS_FAILED)
        raise
    if result.get("paused"):  # parked at a `gate: human` step — durable + resumable, not terminal
        result.setdefault("run_id", run_id)
        result["output"] = _paused_message(name, result)
        return result
    # Failed steps never reach _run_step's step_done (the engine records their error
    # text inline) — mirror that error text into the run record.
    for sid in result["failed"]:
        run_store.step_done(sid, result["steps"][sid], failed=True)
    run_store.finish(STATUS_FAILED if result["failed"] else STATUS_DONE, result)
    result["run_id"] = run_id
    return result


async def _execute(reg: WorkflowRegistry, name: str, inputs: dict, on_step=None, run_store=None) -> dict:
    """Validate → resolve → run a recipe over subagents (each step via the SDK). Raises
    ValueError on unknown/invalid recipe or missing inputs.

    Every execution gets a UUID run_id and a durable audit trail: a ``WorkflowRunStore``
    (default: ``{writable_dir}/.runs/{run_id}.json``) is written on start, at each step
    dispatch and completion, and on finish."""
    recipe, resolved = _prepare(reg, name, inputs)
    if run_store is None:
        run_store = WorkflowRunStore(_writable_dir() / ".runs")
    run_id = run_store.start(name, resolved, steps=recipe.get("steps"))
    run_store.prune(keep=_MAX_RUNS)
    return await _run_prepared(recipe, name, resolved, run_store, run_id, on_step)


async def _start_background(
    reg: WorkflowRegistry,
    name: str,
    inputs: dict,
    notify: Callable[[str, dict], None] | None = None,
    run_store: WorkflowRunStore | None = None,
) -> str:
    """The Studio's run shape: validate NOW (a bad request fails the caller, it never
    mints a failed run record), create the run record, execute detached, return the
    run_id the console polls. ``notify(event, data)`` fires on the terminal/paused
    transition (the plugin event bus — a rail dot when the operator looks away)."""
    recipe, resolved = _prepare(reg, name, inputs)
    if run_store is None:
        run_store = WorkflowRunStore(_writable_dir() / ".runs")
    run_id = run_store.start(name, resolved, steps=recipe.get("steps"))
    run_store.prune(keep=_MAX_RUNS)

    async def _run() -> None:
        try:
            result = await _run_prepared(recipe, name, resolved, run_store, run_id)
        except Exception:  # noqa: BLE001 — the record is already finished(FAILED)
            log.exception("[workflows] background run %s (%s) failed", run_id, name)
            if notify:
                notify("run-finished", {"run_id": run_id, "recipe": name, "status": STATUS_FAILED})
            return
        if notify:
            if result.get("paused"):
                notify("run-paused", {"run_id": run_id, "recipe": name, "paused_step": result.get("paused_step")})
            else:
                status = STATUS_FAILED if result.get("failed") else STATUS_DONE
                notify("run-finished", {"run_id": run_id, "recipe": name, "status": status})

    _spawn(_run())
    return run_id


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


def _resume_precheck(run_store: WorkflowRunStore, run_id: str, action: str, edits: dict | None) -> dict:
    """The resume guards that must fail the CALLER — before anything flips on disk.
    Failing later would orphan the run: gone from the pending list, stuck ``running``,
    unresumable (QA panel blocker). Returns the paused state. Raises ValueError."""
    if action not in ("approve", "edit", "reject"):
        raise ValueError(f"unknown resume action {action!r} (approve | edit | reject)")
    # A non-empty STRING is required. `str(x).strip()` was the old check, but
    # `str(None)` == "None" (truthy), so a JSON `null` prompt slipped past this up-front
    # guard, flipped the run to `running`, then failed a second check too late —
    # orphaning it (#2143). isinstance catches null / missing / non-string here.
    _edit_prompt = (edits or {}).get("prompt")
    if action == "edit" and not (isinstance(_edit_prompt, str) and _edit_prompt.strip()):
        raise ValueError("edit action requires a non-empty edits.prompt")
    state = run_store.load(run_id)
    if state is None:
        raise ValueError(f"no run {run_id!r}")
    if state.get("status") != STATUS_PAUSED:
        raise ValueError(f"run {run_id!r} is not paused (status: {state.get('status')})")
    if not state.get("pending_step"):
        raise ValueError(f"run {run_id!r} has no pending step to resume")
    return state


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
    if run_store is None:
        run_store = WorkflowRunStore(_writable_dir() / ".runs")
    state = _resume_precheck(run_store, run_id, action, edits)
    pending_step = state["pending_step"]

    name = state["recipe_name"]
    recipe = reg.get(name)
    if recipe is None:
        raise ValueError(f"no workflow named {name!r}")
    inputs = dict(state.get("inputs") or {})
    completed = dict(state.get("step_outputs") or {})

    # Re-attach the store to this run and flip it back to `running` before dispatching.
    run_store.resume(run_id)

    async def _run_step(subagent_type: str, prompt: str, step_id: str) -> str:
        run_store.step_started(step_id)
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
        result["output"] = _paused_message(name, result)
        return result
    # Failed steps (inline errors + a reject) never hit _run_step's step_done — mirror
    # their error text into the record, matching _run_prepared's finish path.
    for sid in result["failed"]:
        run_store.step_done(sid, result["steps"][sid], failed=True)
    run_store.finish(STATUS_FAILED if result["failed"] else STATUS_DONE, result)
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

    global _MAX_RUNS
    cfg = getattr(registry, "config", None) or {}
    try:
        _MAX_RUNS = max(1, int(cfg.get("max_runs", _MAX_RUNS)))
    except (TypeError, ValueError):
        pass

    _cache: dict[str, Any] = {"dirs": None, "reg": None}

    def _reg() -> WorkflowRegistry:
        dirs = tuple(str(d) for d in (getattr(STATE, "plugin_workflow_dirs", None) or ()))
        if _cache["reg"] is None or dirs != _cache["dirs"]:
            _cache["dirs"] = dirs
            _cache["reg"] = _build_registry(list(dirs))
        return _cache["reg"]

    def _notify(event: str, data: dict) -> None:
        # Bus broadcast (ADR 0039) — best-effort; a Studio that's open polls anyway.
        try:
            registry.emit(event, data)
        except Exception:  # noqa: BLE001
            pass

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
        if result.get(
            "paused"
        ):  # gated step reached — the full status block (recipe, step, run id, prior outputs, resume paths)
            return _paused_message(name, result)
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
        # Surface `gate: human` steps in the confirmation so the author knows the
        # saved recipe will park at them (the gate rides along verbatim in `steps`).
        gated = [str(s.get("id")) for s in steps if isinstance(s, dict) and s.get("gate") == "human"]
        gate_note = f" Gated step(s) {', '.join(gated)} will pause for operator approval when run." if gated else ""
        return (
            f"Saved workflow {name!r} ({len(steps)} step(s)) to {path}.{gate_note} "
            f"Run it with run_workflow({name!r}, ...)."
        )

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
        # Synchronous — blocks until the DAG completes. The Studio uses /start.
        try:
            return await _execute(_reg(), name, (body or {}).get("inputs") or {})
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @router.post("/{name}/start")
    async def _start_route(name: str, body: dict = Body(default={})) -> dict:
        # The Studio's live-run shape: validate now, run detached, poll GET /runs/{id}.
        try:
            run_id = await _start_background(_reg(), name, (body or {}).get("inputs") or {}, notify=_notify)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {"started": True, "run_id": run_id}

    @router.post("/save")
    async def _save(body: dict = Body(...)) -> dict:
        errs = validate_recipe(body, known_subagents=sdk.subagent_types())
        if errs:
            raise HTTPException(status_code=400, detail="invalid recipe: " + "; ".join(errs))
        path = _reg().save(body)
        return {"saved": True, "name": body.get("name"), "path": path}

    @router.post("/validate")
    async def _validate(body: dict = Body(...)) -> dict:
        # The builder's live validation — the same checks /save enforces, as data
        # instead of a 400, so the UI can show them inline while authoring.
        return {"errors": validate_recipe(body or {}, known_subagents=sdk.subagent_types())}

    @router.get("/runs")
    async def _runs() -> dict:
        # The Pending Gates queue — only PAUSED runs, each with its parked step's
        # rendered prompt + prior outputs. Empty list when nothing is gated.
        return {"runs": _list_paused_runs(_reg())}

    @router.get("/runs/all")
    async def _runs_all(limit: int = 50) -> dict:
        # Run history — summaries of every recorded run (any status), newest first.
        store = WorkflowRunStore(_writable_dir() / ".runs")
        return {"runs": store.recent(limit=limit)}

    @router.get("/runs/{run_id}")
    async def _run_get(run_id: str) -> dict:
        # One run's full record — the Studio's live-timeline polling target and its
        # history inspector (step graph, per-step status/timing, outputs, final envelope).
        store = WorkflowRunStore(_writable_dir() / ".runs")
        state = store.load(run_id)
        if state is None:
            raise HTTPException(status_code=404, detail=f"no run {run_id!r}")
        return state

    @router.post("/runs/{run_id}/resume")
    async def _resume_route(run_id: str, body: dict = Body(default={})) -> dict:
        body = body or {}
        action = body.get("action") or "approve"
        edits = body.get("edits") or {}
        if body.get("background"):
            # Studio shape: precheck now (a bad action/prompt or non-paused run 400s
            # without flipping anything), resume detached, poll GET /runs/{run_id}.
            store = WorkflowRunStore(_writable_dir() / ".runs")
            try:
                _resume_precheck(store, run_id, action, edits)
            except ValueError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc

            async def _bg() -> None:
                try:
                    result = await _resume(_reg(), run_id, action, edits)
                except Exception:  # noqa: BLE001 — record already finished(FAILED)
                    log.exception("[workflows] background resume %s failed", run_id)
                    _notify("run-finished", {"run_id": run_id, "status": STATUS_FAILED})
                    return
                if result.get("paused"):
                    _notify("run-paused", {"run_id": run_id, "paused_step": result.get("paused_step")})
                else:
                    _notify(
                        "run-finished",
                        {"run_id": run_id, "status": STATUS_FAILED if result.get("failed") else STATUS_DONE},
                    )

            _spawn(_bg())
            return {"resumed": True, "run_id": run_id}
        try:
            return await _resume(_reg(), run_id, action, edits)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @router.get("/{name}/recipe")
    async def _recipe(name: str) -> dict:
        # The FULL recipe document (prompts, output template, gates) — what the
        # builder loads to EDIT. `/list` stays a summary. Declared after the /runs
        # routes so a run id never resolves as a recipe name.
        recipe = _reg().get(name)
        if recipe is None:
            raise HTTPException(status_code=404, detail=f"no workflow named {name!r}")
        return {"recipe": recipe}

    @router.delete("/{name}")
    async def _delete(name: str) -> dict:
        return {"deleted": _reg().delete(name)}

    registry.register_router(router, prefix="/api/plugins/workflows")
