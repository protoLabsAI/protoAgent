"""``coder`` — execution-grounded code-solve (ADR 0064).

The missing **verifier-grounded** rung in the Lead Engineer board loop: turn a
*verifiable* coding task into a **test-verified** solution by a difficulty-gated
search ladder (greedy → best-of-k → tree-search → fusion), gated on test pass/fail
rather than an LLM judge.

It **composes** the `delegates` registry (candidate generation) + a subprocess test
runner (the verifier) — it does not reimplement the ACP/A2A spawn primitive. The
deterministic ladder lives in :mod:`plugins.coder.solve`; this module exposes it two
ways over the one engine:

- ``coder_solve`` **tool** — the lead agent (or a subagent) calls it; runs the ladder.
- ``coder`` **subagent** — a thin prompted face over the tool (Subagents panel).

The P2 board seam (``projectBoard-plugin``) calls ``solve()`` directly with a
worktree verifier; see the ADR. Ships **disabled** — it runs model-authored code in
a subprocess (isolation, not a true sandbox), like ``execute_code``.
"""

from __future__ import annotations

import json
import logging

from .generate import make_delegate_generator
from .solve import Budget, solve
from .verify import run_tests

log = logging.getLogger("protoagent.plugins.coder")


def _build_coder_solve_tool(cfg: dict):
    from langchain_core.tools import tool

    delegate_name = str(cfg.get("delegate") or "").strip()
    solution_name = str(cfg.get("solution_name") or "solution").strip() or "solution"
    budget_total = int(cfg.get("budget", 6))
    k = int(cfg.get("k", 3))
    tree_depth = int(cfg.get("tree_depth", 2))
    test_timeout = float(cfg.get("test_timeout", 60.0))
    # Optional rung 4 (ADR 0064): a richer generator (e.g. protolabs/fusion, an openai
    # delegate) tried only after the cheaper rungs fail their tests. Off unless set.
    fusion_delegate = str(cfg.get("fusion_delegate") or "").strip()
    fusion_k = int(cfg.get("fusion_k", 2))

    description = (
        "Solve a VERIFIABLE coding task and return a TEST-VERIFIED solution. Pass `task` "
        "(what to implement) and `tests` (pytest source that imports the solution as "
        f"`from {solution_name} import ...`). Runs an execution-grounded ladder — greedy, "
        "then best-of-k, then refine-on-failing-tests — gated on the tests actually "
        "passing (not a judge). Returns the solution, whether all tests passed, any "
        "failing cases, the rung that solved it, and generations spent. Omit `tests` only "
        "if you have none: it then returns a single un-verified candidate (it shines when "
        "you can supply an oracle)."
    )

    @tool("coder_solve", description=description)
    async def coder_solve(task: str, tests: str = "") -> str:
        if not delegate_name:
            return (
                "coder is not configured: set `coder.delegate` to a declared delegate name "
                "(an openai model endpoint, or an acp coder)."
            )
        if not (task and task.strip()):
            return "coder_solve: `task` is required."
        gen = make_delegate_generator(delegate_name, solution_name=solution_name)
        # Rung 4 fires only when a fusion delegate is configured; otherwise the ladder
        # stops at tree-search. The fusion rung is also test-gated AND budget-gated in
        # solve(), so it pays fusion's ~3× cost only on genuinely hard problems.
        fusion_gen = make_delegate_generator(fusion_delegate, solution_name=solution_name) if fusion_delegate else None
        verify = None
        if tests and tests.strip():
            async def verify(code: str):
                return await run_tests(code, tests, solution_name=solution_name, timeout=test_timeout)

        try:
            result = await solve(
                task,
                generate=gen,
                verify=verify,
                budget=Budget(budget_total),
                k=k,
                tree_depth=tree_depth,
                fusion_generate=fusion_gen,
                fusion_k=fusion_k,
            )
        except Exception as exc:  # noqa: BLE001 — surface as a tool result, not a crash
            log.exception("[coder] solve failed")
            return f"coder_solve failed: {type(exc).__name__}: {exc}"

        payload = {
            "passed": result.passed,
            "rung": result.rung,
            "gens_spent": result.gens_spent,
            "candidates_tried": result.candidates_tried,
            "note": result.note,
            "failing": result.verdict.failing if result.verdict else [],
            "solution": result.solution,
        }
        return json.dumps(payload, indent=2)

    return coder_solve


def register(registry) -> None:
    """Entry point — register the ``coder_solve`` tool + the ``coder`` subagent."""
    cfg = registry.config or {}
    try:
        registry.register_tool(_build_coder_solve_tool(cfg))
    except Exception:  # noqa: BLE001 — a tool build failure shouldn't kill plugin load
        log.exception("[coder] building coder_solve tool failed")
        return

    from .subagent import build_coder_subagent

    sub = build_coder_subagent()
    if sub is not None:
        registry.register_subagent(sub)
    log.info("[coder] registered coder_solve + coder subagent (delegate=%r)", cfg.get("delegate"))
