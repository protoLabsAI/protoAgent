"""Workflows plugin — declarative, multi-step subagent workflows (ADR 0002).

Extracted from core to an **opt-in plugin** (lean core): the engine/registry live here,
and the engine taps core **only via the plugin SDK** (`graph.sdk.run_subagent` +
`subagent_types`) — the first real consumer of the consumption SDK, never importing
`graph.agent` internals. `enabled: false` in the manifest → off by default.

Contributes:
  • tools  `run_workflow`, `save_workflow` (the agent runs/saves recipes)
  • router `/api/plugins/workflows/{list, {name}/run, save, {name}}` (the console Studio surface)
  • recipe dir (its bundled recipes, also exposed to the shared registry per ADR 0027)
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Awaitable, Callable

from langchain_core.tools import tool

from graph import sdk
from plugins.workflows.engine import execute_workflow, resolve_inputs, validate_recipe
from plugins.workflows.registry import WorkflowRegistry

_RECIPES = Path(__file__).parent / "recipes"


def _build_registry(extra_dirs: list[str] | None) -> WorkflowRegistry:
    """Bundled recipes + other enabled plugins' recipe dirs (ADR 0027) + a writable dir
    (user/agent-saved recipes win on a name clash). ``workflow_dir`` config is used
    verbatim when an operator overrides it; the legacy ``/sandbox`` default maps to the
    per-instance ``instance_root/workflows`` store."""
    from infra.paths import instance_paths

    dirs: list[str] = [str(_RECIPES)]
    for d in extra_dirs or []:
        if Path(d).is_dir():
            dirs.append(str(d))
    cfg = sdk.config()
    configured = getattr(cfg, "workflow_dir", "") or ""
    if configured and not str(configured).startswith("/sandbox"):
        writable = Path(configured).expanduser()
    else:
        writable = instance_paths().store("workflows")
    writable.mkdir(parents=True, exist_ok=True)
    dirs.append(str(writable))
    return WorkflowRegistry(dirs, writable_dir=str(writable))


async def _execute(reg: WorkflowRegistry, name: str, inputs: dict, on_step=None) -> dict:
    """Validate → resolve → run a recipe over subagents (each step via the SDK). Raises
    ValueError on unknown/invalid recipe or missing inputs."""
    recipe = reg.get(name)
    if recipe is None:
        raise ValueError(f"no workflow named {name!r}")
    errs = validate_recipe(recipe, known_subagents=sdk.subagent_types())
    if errs:
        raise ValueError("invalid workflow: " + "; ".join(errs))
    resolved, missing = resolve_inputs(recipe, inputs or {})
    if missing:
        raise ValueError(f"missing required input(s): {', '.join(missing)}")

    async def _run_step(subagent_type: str, prompt: str, step_id: str) -> str:
        if on_step:
            await _safe(on_step, {"phase": "start", "step_id": step_id, "subagent": subagent_type})
        out = await sdk.run_subagent(subagent_type, prompt, description=f"workflow {name}:{step_id}")
        if on_step:
            await _safe(on_step, {"phase": "end", "step_id": step_id, "subagent": subagent_type, "output": out})
        return out

    return await execute_workflow(
        recipe,
        resolved,
        run_step=_run_step,
        max_concurrency=getattr(sdk.config(), "subagent_max_concurrency", 3),
    )


async def _safe(cb: Callable[[dict], Awaitable[None]], event: dict) -> None:
    try:
        await cb(event)
    except Exception:  # noqa: BLE001 — progress is best-effort, never fatal
        pass


def register(registry: Any) -> None:
    # Other enabled plugins' recipe dirs are collected on the shared registry (ADR 0027).
    reg = _build_registry(getattr(registry, "workflow_dirs", None))

    # Publish the registry + a runner onto runtime state so core surfaces that predate
    # the plugin (the chat `/<recipe>` slash-command) can use workflows WITHOUT importing
    # this plugin — both are None when the plugin is disabled, which gates those paths.
    from runtime.state import STATE

    async def _run(name: str, inputs: dict | None = None, on_step=None) -> dict:
        return await _execute(reg, name, inputs or {}, on_step)

    STATE.workflow_registry = reg
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
            summaries = reg.list()
            if not summaries:
                return "No workflows are available."
            lines = ["Available workflows:"]
            for s in summaries:
                req = [i["name"] for i in s["inputs"] if i["required"]]
                lines.append(f"- {s['name']}: {s['description']} (inputs: {', '.join(req) or 'none required'})")
            return "\n".join(lines)
        try:
            result = await _execute(reg, name, inputs or {})
        except ValueError as exc:
            return f"Workflow {name!r}: {exc}"
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
                ``prompt`` (may reference {{inputs.x}} / {{steps.<id>.output}}), and
                optional ``depends_on`` (earlier step ids; independent steps run in parallel).
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
            path = reg.save(recipe)
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
        return {"workflows": reg.list()}

    @router.post("/{name}/run")
    async def _run(name: str, body: dict = Body(default={})) -> dict:
        try:
            return await _execute(reg, name, (body or {}).get("inputs") or {})
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @router.post("/save")
    async def _save(body: dict = Body(...)) -> dict:
        errs = validate_recipe(body, known_subagents=sdk.subagent_types())
        if errs:
            raise HTTPException(status_code=400, detail="invalid recipe: " + "; ".join(errs))
        path = reg.save(body)
        return {"saved": True, "name": body.get("name"), "path": path}

    @router.delete("/{name}")
    async def _delete(name: str) -> dict:
        return {"deleted": reg.delete(name)}

    registry.register_router(router, prefix="/api/plugins/workflows")
