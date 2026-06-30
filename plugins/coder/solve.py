"""The execution-grounded solve ladder (ADR 0064).

``solve()`` is the deterministic orchestrator at the heart of ``coder``: a
difficulty-gated escalation ladder that turns a *verifiable* coding task into a
**test-verified** solution. Each rung fires only when the cheaper one fails its
tests:

    1. greedy        1-shot                                   cheap; solves most
    2. best-of-k     k candidates → run tests → select         headroom recovery
    3. tree-search   refine on the *failing* tests, bounded     grounded fix loop
    4. fusion        richer candidates → execute-select         hardest (P3)

The ladder is **pure orchestration** — it depends only on two injected async
callables, so it is fully unit-testable without a live gateway or a subprocess:

- ``generate(prompt, *, feedback) -> str`` — produce one candidate solution.
- ``verify(code) -> Verdict`` — run the tests against a candidate.

The two faces (the ``coder_solve`` tool and the board seam) wire real
implementations (``generate`` over the delegate registry; ``verify`` over the
subprocess test runner / worktree) and call this same engine.

The gate is **test pass/fail**, never an LLM judge — that judge-of-code ceiling
is exactly what this escapes. With no verifier (no tests), the ladder degrades to
**greedy** and says so; it never silently falls back to best-of-k-with-a-judge.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Awaitable, Callable, Optional

# generate(prompt, *, feedback) -> candidate code
Generate = Callable[..., Awaitable[str]]
# verify(code) -> Verdict
Verify = Callable[[str], Awaitable["Verdict"]]


@dataclass
class Verdict:
    """The result of running the tests against one candidate."""

    passed: bool
    total: int = 0
    failed: int = 0
    failing: list[str] = field(default_factory=list)  # named failing cases
    output: str = ""  # raw runner output (truncated by the runner)

    def feedback(self) -> str:
        """A compact, model-facing summary of what failed — the tree-search signal."""
        if self.passed:
            return ""
        head = f"{self.failed}/{self.total} tests failing"
        names = ("\n".join(f"  - {n}" for n in self.failing[:20])) if self.failing else ""
        tail = self.output.strip()[-1500:] if self.output else ""
        return "\n".join(p for p in (head, names, tail) if p)


@dataclass
class SolveResult:
    """What ``solve()`` returns — a verified solution or the best partial."""

    solution: Optional[str]
    passed: Optional[bool]  # True/False, or None when there was no oracle to run
    rung: str  # the rung that produced ``solution`` ("greedy"|"best-of-k"|…|"none")
    gens_spent: int
    candidates_tried: int
    verdict: Optional[Verdict] = None
    note: str = ""


@dataclass
class Budget:
    """A hard cap on generations. ``solve()`` never spends past ``total``."""

    total: int
    _spent: int = 0

    def spend(self, n: int = 1) -> None:
        self._spent += n

    @property
    def spent(self) -> int:
        return self._spent

    @property
    def remaining(self) -> int:
        return max(0, self.total - self._spent)

    def can_afford(self, n: int = 1) -> bool:
        return self.remaining >= n


async def solve(
    task: str,
    *,
    generate: Generate,
    verify: Optional[Verify],
    budget: Budget,
    k: int = 3,
    tree_depth: int = 2,
    fusion_generate: Optional[Generate] = None,
    fusion_k: int = 2,
) -> SolveResult:
    """Run the execution-grounded ladder. See module docstring.

    ``verify=None`` (no oracle) ⇒ greedy 1-shot, returned with ``passed=None`` and
    a scope note — never an un-grounded best-of-k.
    """
    tried = 0

    # ── No oracle: honest greedy degrade ──────────────────────────────────────
    if verify is None:
        if not budget.can_afford(1):
            return SolveResult(None, None, "none", budget.spent, 0, note="budget exhausted before any generation")
        code = await generate(task, feedback=None)
        budget.spend(1)
        return SolveResult(
            code,
            None,
            "greedy",
            budget.spent,
            1,
            note="no verifier/tests supplied — returned a single un-verified candidate (coder shines on verifiable tasks)",
        )

    # ── Rung 1: greedy ────────────────────────────────────────────────────────
    if not budget.can_afford(1):
        # No generation happened, so the oracle never ran ⇒ passed=None ("unknown"),
        # not False ("ran and failed") — keep the SolveResult.passed contract honest.
        return SolveResult(None, None, "none", budget.spent, 0, note="budget exhausted before any generation")
    code = await generate(task, feedback=None)
    budget.spend(1)
    tried += 1
    verdict = await verify(code)
    if verdict.passed:
        return SolveResult(code, True, "greedy", budget.spent, tried, verdict, "solved 1-shot")
    best, best_verdict = code, verdict  # carry the best partial for refinement/return

    # ── Rung 2: best-of-k ─────────────────────────────────────────────────────
    n = min(k - 1, budget.remaining)  # we already spent one greedy gen
    if n > 0:
        cands = await asyncio.gather(*(generate(task, feedback=None) for _ in range(n)))
        budget.spend(n)
        tried += n
        verdicts = await asyncio.gather(*(verify(c) for c in cands))
        for c, v in zip(cands, verdicts):
            if v.passed:
                return SolveResult(c, True, "best-of-k", budget.spent, tried, v, f"solved by best-of-{k}")
            if v.failed < best_verdict.failed:  # fewer failures = better partial
                best, best_verdict = c, v

    # ── Rung 3: tree-search — refine on the failing tests ─────────────────────
    for depth in range(tree_depth):
        if not budget.can_afford(1):
            break
        refined = await generate(task, feedback=best_verdict.feedback())
        budget.spend(1)
        tried += 1
        v = await verify(refined)
        if v.passed:
            return SolveResult(refined, True, "tree-search", budget.spent, tried, v, f"solved by refine@{depth + 1}")
        # `<=` (vs `<` in best-of-k/fusion) is deliberate: a refine chain continues
        # from the LATEST attempt's feedback, so an equal-failure refinement still
        # advances `best` to keep the next round's feedback consistent with it.
        if v.failed <= best_verdict.failed:
            best, best_verdict = refined, v

    # ── Rung 4: fusion — richest proposals, oracle-selected (P3) ──────────────
    if fusion_generate is not None and budget.can_afford(1):
        fk = min(fusion_k, budget.remaining)
        cands = await asyncio.gather(*(fusion_generate(task, feedback=best_verdict.feedback()) for _ in range(fk)))
        budget.spend(fk)
        tried += fk
        verdicts = await asyncio.gather(*(verify(c) for c in cands))
        for c, v in zip(cands, verdicts):
            if v.passed:
                return SolveResult(c, True, "fusion", budget.spent, tried, v, f"solved by fusion (k={fk})")
            if v.failed < best_verdict.failed:
                best, best_verdict = c, v

    # ── Exhausted: return the best partial, naming the failing cases ──────────
    return SolveResult(
        best,
        False,
        "best-partial",
        budget.spent,
        tried,
        best_verdict,
        f"no candidate passed within budget; best partial has {best_verdict.failed}/{best_verdict.total} failing",
    )
