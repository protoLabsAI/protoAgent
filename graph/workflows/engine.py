"""Workflow engine — validate + execute a declarative workflow recipe.

A recipe is a dict (parsed from YAML):

    name: str
    description: str (optional)
    inputs: [{name, required?, default?}]   (optional)
    steps:  [{id, subagent, prompt, depends_on?}]
    output: str (optional template; default = last step's output)

Execution resolves the ``depends_on`` DAG, runs steps whose deps are satisfied
**in parallel** (bounded by a semaphore), threads each step's output into
later prompts via ``{{inputs.x}}`` / ``{{steps.id.output}}`` substitution, and
returns the rendered ``output``. The engine is decoupled from the subagent
runner via the injected ``run_step`` callback, so it's unit-testable.
"""

from __future__ import annotations

import asyncio
import re
from typing import Any, Awaitable, Callable

# {{ inputs.name }} | {{ steps.id.output }}
_REF_RE = re.compile(r"\{\{\s*([a-zA-Z0-9_.]+)\s*\}\}")


def _refs(text: str) -> list[str]:
    return _REF_RE.findall(text or "")


def validate_recipe(recipe: dict, *, known_subagents: set[str] | None = None) -> list[str]:
    """Return a list of human-readable validation errors ([] = valid)."""
    errors: list[str] = []
    if not isinstance(recipe, dict):
        return ["recipe must be a mapping"]
    if not isinstance(recipe.get("name"), str) or not recipe["name"].strip():
        errors.append("missing 'name'")
    steps = recipe.get("steps")
    if not isinstance(steps, list) or not steps:
        return errors + ["'steps' must be a non-empty list"]

    input_names = {i.get("name") for i in (recipe.get("inputs") or []) if isinstance(i, dict)}
    ids: list[str] = []
    for n, step in enumerate(steps):
        if not isinstance(step, dict):
            errors.append(f"step #{n + 1} must be a mapping")
            continue
        sid = step.get("id")
        if not isinstance(sid, str) or not sid.strip():
            errors.append(f"step #{n + 1} missing 'id'")
        elif sid in ids:
            errors.append(f"duplicate step id {sid!r}")
        else:
            ids.append(sid)
        if not isinstance(step.get("subagent"), str) or not step["subagent"].strip():
            errors.append(f"step {sid!r}: missing 'subagent'")
        elif known_subagents is not None and step["subagent"] not in known_subagents:
            errors.append(f"step {sid!r}: unknown subagent {step['subagent']!r}")
        if not isinstance(step.get("prompt"), str) or not step["prompt"].strip():
            errors.append(f"step {sid!r}: missing 'prompt'")

    id_set = set(ids)
    # depends_on references + cycle check
    for step in steps:
        if not isinstance(step, dict):
            continue
        for dep in step.get("depends_on", []) or []:
            if dep not in id_set:
                errors.append(f"step {step.get('id')!r}: depends_on unknown step {dep!r}")
    if not errors and _has_cycle(steps):
        errors.append("steps form a dependency cycle")

    # template references must resolve to a declared input or an existing step output
    for text in [s.get("prompt", "") for s in steps if isinstance(s, dict)] + [recipe.get("output", "")]:
        for ref in _refs(text):
            if ref.startswith("inputs."):
                if ref[len("inputs."):] not in input_names:
                    errors.append(f"template references unknown input {ref!r}")
            elif ref.startswith("steps.") and ref.endswith(".output"):
                mid = ref[len("steps."):-len(".output")]
                if mid not in id_set:
                    errors.append(f"template references unknown step {mid!r}")
            else:
                errors.append(f"unrecognized template reference {ref!r}")
    return errors


def _has_cycle(steps: list[dict]) -> bool:
    graph = {s["id"]: set(s.get("depends_on", []) or []) for s in steps if isinstance(s, dict) and s.get("id")}
    state: dict[str, int] = {}  # 0=visiting, 1=done

    def visit(node: str) -> bool:
        if state.get(node) == 1:
            return False
        if node in state:  # currently visiting → back-edge → cycle
            return True
        state[node] = 0
        for dep in graph.get(node, ()):  # dep must finish before node
            if dep in graph and visit(dep):
                return True
        state[node] = 1
        return False

    return any(visit(n) for n in graph)


def render_template(text: str, inputs: dict, step_outputs: dict) -> str:
    def sub(m: re.Match) -> str:
        ref = m.group(1)
        if ref.startswith("inputs."):
            return str(inputs.get(ref[len("inputs."):], ""))
        if ref.startswith("steps.") and ref.endswith(".output"):
            return str(step_outputs.get(ref[len("steps."):-len(".output")], ""))
        return m.group(0)
    return _REF_RE.sub(sub, text or "")


def resolve_inputs(recipe: dict, provided: dict) -> tuple[dict, list[str]]:
    """Merge provided inputs with declared defaults; return (inputs, missing)."""
    resolved: dict[str, Any] = {}
    missing: list[str] = []
    provided = provided or {}
    for spec in recipe.get("inputs", []) or []:
        if not isinstance(spec, dict) or "name" not in spec:
            continue
        name = spec["name"]
        if name in provided and provided[name] not in (None, ""):
            resolved[name] = provided[name]
        elif "default" in spec:
            resolved[name] = spec["default"]
        elif spec.get("required"):
            missing.append(name)
    # pass through any extra provided inputs too
    for k, v in provided.items():
        resolved.setdefault(k, v)
    return resolved, missing


async def execute_workflow(
    recipe: dict,
    inputs: dict,
    *,
    run_step: Callable[[str, str, str], Awaitable[str]],
    max_concurrency: int = 4,
) -> dict:
    """Run the recipe's step DAG. ``run_step(subagent, prompt, step_id) -> output``.

    Returns ``{"output": str, "steps": {id: output}, "failed": [ids]}``. Step
    failures are recorded inline (the step's output becomes the error text) so
    independent branches still complete — matching task_batch semantics.
    """
    steps = recipe["steps"]
    by_id = {s["id"]: s for s in steps}
    pending = {s["id"]: set(s.get("depends_on", []) or []) for s in steps}
    done: dict[str, str] = {}
    failed: list[str] = []
    sem = asyncio.Semaphore(max(1, max_concurrency))

    async def run_one(sid: str) -> tuple[str, str, bool]:
        step = by_id[sid]
        prompt = render_template(step["prompt"], inputs, done)
        async with sem:
            try:
                out = await run_step(step["subagent"], prompt, sid)
                return sid, str(out), False
            except Exception as exc:  # noqa: BLE001 — record inline, keep the DAG going
                return sid, f"Error: step {sid!r} raised {type(exc).__name__}: {exc}", True

    while pending:
        ready = [sid for sid, deps in pending.items() if deps <= set(done)]
        if not ready:  # should be impossible post-validate (cycle) — guard anyway
            for sid in pending:
                done[sid] = f"Error: step {sid!r} skipped (unsatisfiable dependencies)"
                failed.append(sid)
            break
        for sid, out, err in await asyncio.gather(*(run_one(s) for s in ready)):
            done[sid] = out
            if err:
                failed.append(sid)
        for sid in ready:
            pending.pop(sid)

    output_tpl = recipe.get("output") or f"{{{{steps.{steps[-1]['id']}.output}}}}"
    return {"output": render_template(output_tpl, inputs, done), "steps": done, "failed": failed}
