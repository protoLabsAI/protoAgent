"""Goal verifiers — the testable-outcome backing for goal mode.

Each verifier is ``async verify(spec, ctx) -> VerifyResult``. ``spec`` is the
goal's ``verifier`` dict (``type`` + params); ``ctx`` carries the runtime
(config + last-turn transcript) the verifier may need. Look up by
``spec["type"]`` in ``VERIFIERS``.

Types:
  command — run a shell command; exit 0 = met. The generic escape hatch.
  test    — like command, but surfaces the runner's summary line as the reason.
  ci      — GitHub CI status via `gh` (a PR's checks, or a branch's latest run).
  data    — assert over a file: a substring (`contains`) or a restricted
            Python expression (`expr`) over parsed JSON (namespace {"data": ...}).
  llm     — fallback judgment over the transcript (protocli-style) for fuzzy
            goals with no mechanical check. Fails safe (not met) on any error.

Security: command/test/ci verifiers execute on the server host. Goals are an
operator action — only set goals from trusted input. See docs/guides/goal-mode.
"""

from __future__ import annotations

import ast
import json
import logging
from dataclasses import dataclass

from graph.goals.types import VerifyResult

log = logging.getLogger(__name__)

_EVIDENCE_CAP = 2000

# Curated builtins for the `data` verifier's `expr` — common read-only helpers,
# none that touch the filesystem / import / execute code.
_SAFE_BUILTINS = {
    name: __builtins__[name] if isinstance(__builtins__, dict) else getattr(__builtins__, name)
    for name in (
        "len",
        "any",
        "all",
        "sum",
        "min",
        "max",
        "sorted",
        "abs",
        "round",
        "bool",
        "int",
        "float",
        "str",
        "list",
        "dict",
        "set",
        "tuple",
        "isinstance",
        "enumerate",
        "zip",
        "range",
        "map",
        "filter",
    )
}

# Attribute access is the eval-sandbox escape (``().__class__.__bases__[0].
# __subclasses__()`` and the ``"{0.__class__}".format`` / f-string variants), so
# the `data` verifier's expr is AST-validated to reject it (and dunder names,
# lambda, await/yield) BEFORE evaluation — subscripts, comparisons, boolean/
# arithmetic ops, comprehensions and calls to the curated builtins still work.
_DISALLOWED_EXPR_NODES = (ast.Attribute, ast.Lambda, ast.Await, ast.Yield, ast.YieldFrom)


def _eval_data_expr(expr: str, data):
    """Evaluate a `data` verifier expr with the escape vectors statically rejected
    and only the curated read-only builtins available. Raises ``ValueError`` on a
    disallowed construct, otherwise returns the expr's value."""
    tree = ast.parse(expr, mode="eval")
    for node in ast.walk(tree):
        if isinstance(node, _DISALLOWED_EXPR_NODES):
            raise ValueError(f"{type(node).__name__} is not allowed in a data expr")
        if isinstance(node, ast.Name) and node.id.startswith("__"):
            raise ValueError(f"dunder name {node.id!r} is not allowed in a data expr")
    code = compile(tree, "<data-expr>", "eval")
    return eval(code, {"__builtins__": _SAFE_BUILTINS}, {"data": data})  # noqa: S307 — AST-validated above


@dataclass
class VerifyContext:
    config: object = None
    condition: str = ""  # the goal condition (used by the llm verifier)
    last_text: str = ""  # last assistant message of the turn
    tool_summary: str = ""  # short summary of recent tool calls
    cwd: str | None = None  # working dir for command/test verifiers


def _tail(text: str, cap: int = _EVIDENCE_CAP) -> str:
    text = (text or "").strip()
    return text if len(text) <= cap else "…" + text[-cap:]


async def _verify_command(spec: dict, ctx: VerifyContext) -> VerifyResult:
    from tools.shell import run_command

    command = spec.get("command")
    if not command:
        return VerifyResult(False, "command verifier missing 'command'", "")
    timeout = float(spec.get("timeout") or getattr(ctx.config, "goal_verify_timeout", 120))
    cwd = spec.get("cwd") or ctx.cwd
    res = await run_command(["bash", "-c", command], timeout=timeout, cwd=cwd)
    evidence = _tail("\n".join(p for p in (res.stdout, res.stderr) if p))
    if res.error:
        return VerifyResult(False, f"command could not run: {res.error}", evidence)
    if res.timed_out:
        return VerifyResult(False, f"command timed out after {timeout:g}s", evidence)
    if res.returncode == 0:
        return VerifyResult(True, "command exited 0", evidence)
    return VerifyResult(False, f"command exited {res.returncode}", evidence)


async def _verify_test(spec: dict, ctx: VerifyContext) -> VerifyResult:
    res = await _verify_command(spec, ctx)
    # Surface the runner's last meaningful line (e.g. "5 passed in 1.2s").
    last_line = ""
    for line in reversed((res.evidence or "").splitlines()):
        if line.strip():
            last_line = line.strip()
            break
    if last_line:
        res.reason = f"{res.reason} — {last_line}"
    return res


async def _verify_ci(spec: dict, ctx: VerifyContext) -> VerifyResult:
    from tools.gh_cli import run_gh

    pr = spec.get("pr")
    branch = spec.get("branch")
    if pr is not None:
        rc, out, err = await run_gh(["pr", "checks", str(pr)])
        evidence = _tail("\n".join(p for p in (out, err) if p))
        # `gh pr checks` exits 0 only when all checks completed successfully.
        if rc == 0:
            return VerifyResult(True, f"PR #{pr} checks all green", evidence)
        return VerifyResult(False, f"PR #{pr} checks not all green (gh exit {rc})", evidence)
    if branch:
        rc, out, err = await run_gh(
            [
                "run",
                "list",
                "--branch",
                str(branch),
                "--limit",
                "1",
                "--json",
                "conclusion,status,name",
            ]
        )
        evidence = _tail("\n".join(p for p in (out, err) if p))
        if rc != 0:
            return VerifyResult(False, f"gh run list failed (exit {rc})", evidence)
        try:
            runs = json.loads(out or "[]")
        except json.JSONDecodeError:
            return VerifyResult(False, "could not parse gh run list output", evidence)
        if not runs:
            return VerifyResult(False, f"no CI runs found for branch {branch}", evidence)
        run = runs[0]
        status = run.get("status")
        conclusion = run.get("conclusion")
        if status != "completed":
            return VerifyResult(False, f"latest CI run is {status}", evidence)
        if conclusion == "success":
            return VerifyResult(True, f"latest CI run on {branch} succeeded", evidence)
        return VerifyResult(False, f"latest CI run concluded {conclusion}", evidence)
    return VerifyResult(False, "ci verifier needs 'pr' or 'branch'", "")


async def _verify_data(spec: dict, ctx: VerifyContext) -> VerifyResult:
    path = spec.get("path")
    if not path:
        return VerifyResult(False, "data verifier missing 'path'", "")
    try:
        with open(path, encoding="utf-8") as fh:
            text = fh.read()
    except OSError as exc:
        return VerifyResult(False, f"cannot read {path}: {exc}", "")

    if "contains" in spec:
        needle = str(spec["contains"])
        met = needle in text
        return VerifyResult(met, f"{'found' if met else 'missing'} substring", _tail(text))

    expr = spec.get("expr")
    if expr:
        try:
            data = json.loads(text)
        except json.JSONDecodeError as exc:
            return VerifyResult(False, f"{path} is not valid JSON: {exc}", _tail(text))
        try:
            # AST-validated restricted eval: curated read-only builtins + the
            # parsed document as `data`, with attribute access / dunder names
            # rejected so the expr can't escape the sandbox. See _eval_data_expr.
            result = _eval_data_expr(expr, data)
        except Exception as exc:
            return VerifyResult(False, f"expr error: {type(exc).__name__}: {exc}", _tail(text))
        met = bool(result)
        return VerifyResult(met, f"expr -> {result!r}", _tail(text))

    return VerifyResult(False, "data verifier needs 'contains' or 'expr'", _tail(text))


_LLM_SYSTEM = (
    "You are a strict goal evaluator. Decide whether the GOAL is *visibly "
    "demonstrated* as complete by the agent's latest work. Be conservative: "
    "only answer met=true when the transcript shows concrete evidence (results, "
    "outputs, confirmations). If evidence is missing or partial, answer "
    "met=false with a one-sentence reason naming what's still needed. "
    'Respond ONLY with JSON: {"met": true|false, "reason": "<one sentence>"}.'
)


async def _verify_llm(spec: dict, ctx: VerifyContext) -> VerifyResult:
    if ctx.config is None:
        return VerifyResult(False, "llm verifier unavailable (no config)", "")
    try:
        from langchain_core.messages import HumanMessage, SystemMessage

        from graph.llm import create_llm

        # Goal verification is classification, not the main reasoning task:
        # eval_model override → routing.aux_model → main model.
        model_name = getattr(ctx.config, "goal_eval_model", "") or getattr(ctx.config, "aux_model", "") or None
        llm = create_llm(ctx.config, model_name=model_name)
        prompt = (
            f"GOAL: {spec.get('condition') or ctx.condition}\n\n"
            f"Recent tool calls:\n{ctx.tool_summary or '(none)'}\n\n"
            f"Agent's latest message:\n{ctx.last_text or '(empty)'}"
        )
        resp = await llm.ainvoke(
            [SystemMessage(content=_LLM_SYSTEM), HumanMessage(content=prompt)],
            config={"temperature": 0},
        )
        content = resp.content if isinstance(resp.content, str) else str(resp.content)
        # Tolerate fenced/extra text — grab the first JSON object.
        start, end = content.find("{"), content.rfind("}")
        if start == -1 or end == -1:
            return VerifyResult(False, "evaluator returned no JSON", _tail(content))
        parsed = json.loads(content[start : end + 1])
        return VerifyResult(bool(parsed.get("met")), str(parsed.get("reason") or ""), "")
    except Exception as exc:  # fail safe: never let evaluator errors mark met
        log.warning("[goal] llm verifier error: %s", exc)
        return VerifyResult(False, f"evaluator error: {type(exc).__name__}", "")


# Plugin-contributed verifiers (ADR 0028), keyed by their namespaced name
# (``<plugin-id>:<name>``). Populated by the loader at graph build via
# ``set_plugin_verifiers`` (re-set on config reload). In-process, reviewed code
# with declarative args — no shell, no eval (that's why a ``plugin`` goal is the
# only verifier type safe to set programmatically; see ADR 0028 D3).
_PLUGIN_VERIFIERS: dict = {}


def set_plugin_verifiers(mapping: dict | None) -> None:
    """Replace the registered plugin verifier set (called at build + reload).

    Rebinds the module global in ONE atomic step rather than ``clear()`` then
    ``update()``. The old two-step left a window where the dict was momentarily
    empty, so a concurrent monitor-goal tick's ``_verify_plugin`` lookup could land
    mid-swap and return a spurious ``unknown plugin verifier`` — which surfaced as
    goals intermittently flashing "unknown" after a reload, then recovering on the
    next tick. A single name rebind is GIL-atomic; readers see the old or new map,
    never an empty one. (``_PLUGIN_VERIFIERS`` is referenced only within this module,
    so reassigning the global is safe.)"""
    global _PLUGIN_VERIFIERS
    _PLUGIN_VERIFIERS = dict(mapping or {})


def plugin_verifier_names() -> list[str]:
    """Registered plugin-verifier names (``<plugin-id>:<name>``), sorted. Lets the
    set_goal tool reject an unknown verifier before creating an unsatisfiable goal."""
    return sorted(_PLUGIN_VERIFIERS)


async def _verify_plugin(spec: dict, ctx: VerifyContext) -> VerifyResult:
    """Dispatch a ``{"type":"plugin","check":"<id>:<name>","args":{…}}`` goal to a
    plugin-registered verifier. ``args`` are declarative data the verifier validates."""
    name = (spec or {}).get("check") or ""
    fn = _PLUGIN_VERIFIERS.get(name)
    if fn is None:
        return VerifyResult(False, f"unknown plugin verifier {name!r}", "")
    try:
        return await fn(spec, ctx)
    except Exception as exc:  # a bad verifier must never mark a goal met
        log.warning("[goal] plugin verifier %s error: %s", name, exc)
        return VerifyResult(False, f"verifier error: {type(exc).__name__}", "")


VERIFIERS = {
    "command": _verify_command,
    "test": _verify_test,
    "ci": _verify_ci,
    "data": _verify_data,
    "llm": _verify_llm,
    "plugin": _verify_plugin,
}


async def run_verifier(spec: dict, ctx: VerifyContext) -> VerifyResult:
    """Dispatch to the verifier named by ``spec['type']`` (default 'llm')."""
    vtype = (spec or {}).get("type", "llm")
    fn = VERIFIERS.get(vtype)
    if fn is None:
        return VerifyResult(False, f"unknown verifier type {vtype!r}", "")
    # The llm verifier wants the condition; pass it through the spec view.
    if vtype == "llm" and "condition" not in spec:
        spec = {**spec, "condition": getattr(ctx, "condition", "")}
    return await fn(spec, ctx)
