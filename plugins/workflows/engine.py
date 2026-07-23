"""Workflow engine — validate + execute a declarative workflow recipe.

A recipe is a dict (parsed from YAML):

    name: str
    description: str (optional)
    inputs: [{name, required?, default?}]   (optional)
    steps:  [{id, subagent, prompt, depends_on?}]
    output: str (optional template; default = last step's output)
    max_concurrency: int (optional; the recipe's fan-out width — see below)

Execution resolves the ``depends_on`` DAG, runs steps whose deps are satisfied
**in parallel** (bounded by a semaphore), threads each step's output into
later prompts via ``{{inputs.x}}`` / ``{{steps.id.output}}`` substitution, and
returns the rendered ``output``. The engine is decoupled from the subagent
runner via the injected ``run_step`` callback, so it's unit-testable.

A step may carry an optional ``gate`` (only ``human`` is accepted for now). The
engine itself is gate-agnostic: it consults the injected ``gate_check`` callback
before dispatching each ready step, and if that returns ``"pause"`` it parks the
run (via ``pause_fn``) and returns a paused envelope instead of running the step —
so the gated step's subagent is never spawned. When ``gate_check`` is ``None`` the
loop is byte-for-byte the pre-gate path.
"""

from __future__ import annotations

import asyncio
import re
import time
from typing import Any, Awaitable, Callable

# {{ inputs.name }} | {{ steps.id.output }}
_REF_RE = re.compile(r"\{\{\s*([a-zA-Z0-9_.]+)\s*\}\}")


def _refs(text: str) -> list[str]:
    return _REF_RE.findall(text or "")


# Ceiling on a recipe-declared fan-out. High enough for any realistic panel, low
# enough that a typo (``max_concurrency: 500``) can't stampede the gateway.
MAX_FANOUT = 16


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
    width = recipe.get("max_concurrency")
    if width is not None and (not isinstance(width, int) or isinstance(width, bool) or not 1 <= width <= MAX_FANOUT):
        errors.append(f"'max_concurrency' must be an int in 1..{MAX_FANOUT}")

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
        gate = step.get("gate")
        if gate is not None and gate != "human":
            errors.append(f"step {sid!r}: unsupported gate {gate!r} (only 'human' is supported)")

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
                if ref[len("inputs.") :] not in input_names:
                    errors.append(f"template references unknown input {ref!r}")
            elif ref.startswith("steps.") and ref.endswith(".output"):
                mid = ref[len("steps.") : -len(".output")]
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
            return str(inputs.get(ref[len("inputs.") :], ""))
        if ref.startswith("steps.") and ref.endswith(".output"):
            return str(step_outputs.get(ref[len("steps.") : -len(".output")], ""))
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
    gate_check: Callable[[dict], str | None] | None = None,
    pause_fn: Callable[[str, dict], str | None] | None = None,
    seed_outputs: dict[str, str] | None = None,
    prompt_overrides: dict[str, str] | None = None,
    prefailed: dict[str, str] | None = None,
    skip_gate: set[str] | None = None,
) -> dict:
    """Run the recipe's step DAG. ``run_step(subagent, prompt, step_id) -> output``.

    Returns ``{"output": str, "steps": {id: output}, "failed": [ids],
    "timings": {id: seconds}}``. Step failures are recorded inline (the step's output
    becomes the error text) so independent branches still complete — matching
    task_batch semantics.

    A recipe's own ``max_concurrency`` wins over the caller's, so a declared fan-out is
    never serialized by a resource cap that knows nothing about this recipe's shape.

    ``gate_check(step_dict) -> "pause" | None`` (optional) is consulted for each
    ready step *before* it is dispatched. When it returns ``"pause"`` the run is
    parked: ``pause_fn(step_id, completed_outputs)`` persists the paused state and
    returns the run_id, and the engine returns ``{"paused": True, "paused_step":
    step_id, "run_id": run_id, "steps": {...done...}, "timings": {...}}`` instead of
    the normal envelope — the gated step's subagent is never spawned. Sequential gated steps
    pause one at a time (a downstream gated step isn't ready until its deps run).
    When ``gate_check`` is ``None`` the loop below is the exact pre-gate path.

    **Resume** (F3): a paused run is continued by re-invoking with the stored state.
    ``seed_outputs`` pre-loads already-completed steps (they are treated as done and
    never re-dispatched); ``skip_gate`` names step ids whose ``gate: human`` is
    bypassed (the operator already approved them); ``prompt_overrides`` substitutes a
    step's prompt verbatim (an *edited* resume); ``prefailed`` pre-records a step as
    failed with the given error text (a *rejected* resume) so its dependents inherit
    the error — exactly like an inline failure. All four default to empty, so a
    from-scratch run is byte-for-byte the pre-resume path.
    """
    steps = recipe["steps"]
    # A recipe's declared fan-out width wins over the caller's default: the caller's cap
    # is a resource guard that knows nothing about this recipe's shape, and a parallel
    # stage wider than it gets silently serialized into waves (5 steps under a cap of 4
    # runs 4+1 and pays twice the slowest step for nothing).
    declared = recipe.get("max_concurrency")
    if isinstance(declared, int) and not isinstance(declared, bool) and 1 <= declared <= MAX_FANOUT:
        max_concurrency = declared
    by_id = {s["id"]: s for s in steps}
    pending = {s["id"]: set(s.get("depends_on", []) or []) for s in steps}
    done: dict[str, str] = dict(seed_outputs or {})
    failed: list[str] = []
    prompt_overrides = prompt_overrides or {}
    skip_gate = set(skip_gate or ())
    sem = asyncio.Semaphore(max(1, max_concurrency))

    # Resume-reject: pre-record the rejected step's error inline so its dependents see
    # it (like a normal inline failure). Seeded + rejected steps are already resolved —
    # drop them from the pending set so the DAG picks up from where it paused.
    for sid, err in (prefailed or {}).items():
        if sid in by_id:
            done[sid] = err
            if sid not in failed:
                failed.append(sid)
    for sid in list(pending):
        if sid in done:
            pending.pop(sid)

    timings: dict[str, float] = {}

    async def run_one(sid: str) -> tuple[str, str, bool]:
        step = by_id[sid]
        prompt = prompt_overrides[sid] if sid in prompt_overrides else render_template(step["prompt"], inputs, done)
        async with sem:
            started = time.monotonic()
            try:
                out = await run_step(step["subagent"], prompt, sid)
                return sid, str(out), False
            except Exception as exc:  # noqa: BLE001 — record inline, keep the DAG going
                return sid, f"Error: step {sid!r} raised {type(exc).__name__}: {exc}", True
            finally:
                # Time spent RUNNING, not queued behind the semaphore — otherwise a
                # serialized wave reads as a slow step and hides the width problem.
                timings[sid] = round(time.monotonic() - started, 2)

    while pending:
        ready = [sid for sid, deps in pending.items() if deps <= set(done)]
        if not ready:  # should be impossible post-validate (cycle) — guard anyway
            for sid in pending:
                done[sid] = f"Error: step {sid!r} skipped (unsatisfiable dependencies)"
                failed.append(sid)
            break
        if gate_check is not None:
            gated = next(
                (sid for sid in ready if sid not in skip_gate and gate_check(by_id[sid]) == "pause"),
                None,
            )
            if gated is not None:  # park BEFORE spawning any subagent → no wasted work
                run_id = pause_fn(gated, done) if pause_fn is not None else None
                return {
                    "paused": True,
                    "paused_step": gated,
                    "run_id": run_id,
                    "steps": dict(done),
                    # Steps that ran before the gate are timed; omitting them here made
                    # the two completion paths disagree about the result shape, and a
                    # long step before a pause is exactly what you want to see.
                    "timings": dict(timings),
                }
        for sid, out, err in await asyncio.gather(*(run_one(s) for s in ready)):
            done[sid] = out
            if err:
                failed.append(sid)
        for sid in ready:
            pending.pop(sid)

    output_tpl = recipe.get("output") or f"{{{{steps.{steps[-1]['id']}.output}}}}"
    return {
        "output": render_template(output_tpl, inputs, done),
        "steps": done,
        "failed": failed,
        "timings": timings,
    }
