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

``force_rung`` (an operator/testing affordance, not something the ladder ever sets
itself): run exactly ONE named rung once — no cheaper-rung-first cascade, no
escalation past it — and report pass/fail. Verifying a specific rung (especially
fusion, only otherwise reached after three cheaper rungs fail) shouldn't require
contriving a task hard enough to fail its way there. A forced ``tree-search`` seeds
one un-refined attempt first (purely to generate the failing-test feedback the
refine step needs) — that seed itself isn't scored as the rung's result unless it
happens to pass outright.
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
    candidates: list[Candidate] = field(default_factory=list)


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


FORCEABLE_RUNGS = ("greedy", "best-of-k", "tree-search", "fusion")


@dataclass
class Candidate:
    """Started work, retained even when verification raises or is cancelled.

    Board adapters own worktree retention and subprocess cleanup. They may supply
    a journal to inspect on exceptions, and must retain partial work themselves
    when generation is interrupted before returning its worktree handle.
    """

    index: int
    solution: Optional[str] = None
    verdict: Optional[Verdict] = None
    error: Optional[BaseException] = None


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
    force_rung: Optional[str] = None,
    generation_concurrency: int = 1,
    verification_concurrency: int = 1,
    verification_semaphore: Optional[asyncio.Semaphore] = None,
    full_batch: bool = False,
    candidates: Optional[list[Candidate]] = None,
) -> SolveResult:
    """Run the ladder with bounded generate→verify pipelines.

    ``generation_concurrency`` bounds whole pipelines, not just dispatch. Existing
    callers default to serial pipelines, including adapters with a session cap of
    one. Higher limits must respect the adapter's operator cap. Verification has
    its own limit; a shared ``verification_semaphore`` additionally bounds testing
    across solves (neither limit controls workers inside a test command).

    A pass stops new alternatives; started siblings finish and are retained. The
    earliest-started passing candidate wins, independent of completion order.
    ``full_batch=True`` explicitly opts into comparing all requested alternatives.
    Failures feed subsequent launches. Exceptions stop launches, drain siblings,
    then propagate. Cancellation (including an enclosing asyncio.timeout deadline)
    cancels and joins every pipeline before propagating; injected callbacks must
    clean up their processes and preserve worktrees in their cancellation handlers.
    Started generations are charged even if they fail or are cancelled. Supply a
    ``candidates`` journal to recover completed handles on exceptional exits.
    """
    if generation_concurrency < 1 or verification_concurrency < 1:
        raise ValueError("concurrency limits must be positive")
    if force_rung is not None:
        if force_rung not in FORCEABLE_RUNGS:
            raise ValueError(f"force_rung must be one of {FORCEABLE_RUNGS}, got {force_rung!r}")
        if force_rung == "fusion" and fusion_generate is None:
            raise ValueError("force_rung='fusion' requires fusion_generate to be configured")
        if verify is None:
            raise ValueError("force_rung requires a verifier — there's nothing to test a rung against otherwise")

    journal = candidates if candidates is not None else []
    started = len(journal)
    verification_slots = asyncio.Semaphore(verification_concurrency)
    best: Optional[Candidate] = None

    async def attempt(gen: Generate, feedback: Optional[str]) -> Candidate:
        # No await between checking the shared budget and charging started work.
        if not budget.can_afford():
            raise RuntimeError("generation budget exhausted")
        budget.spend()
        candidate = Candidate(len(journal))
        journal.append(candidate)
        try:
            candidate.solution = await gen(task, feedback=feedback)
            if verify is not None:
                async with verification_slots:
                    if verification_semaphore is None:
                        candidate.verdict = await verify(candidate.solution)
                    else:
                        async with verification_semaphore:
                            candidate.verdict = await verify(candidate.solution)
            return candidate
        except BaseException as exc:
            candidate.error = exc
            raise

    async def batch(gen: Generate, count: int, *, refine: bool = False) -> Optional[Candidate]:
        nonlocal best
        pending = set()
        completed = []
        errors = []
        launched = 0
        stop = False
        try:
            while pending or (launched < count and budget.can_afford() and not stop):
                # Reserve only actual slots. Never queue the entire batch behind
                # an adapter's dispatch semaphore.
                slots = min(generation_concurrency - len(pending), budget.remaining, count - launched)
                if not stop:
                    for _ in range(slots):
                        feedback = best.verdict.feedback() if best and best.verdict else None
                        pending.add(asyncio.create_task(attempt(gen, feedback)))
                        launched += 1
                    # Let starts charge their budget before considering more work.
                if not pending:
                    break
                done, pending = await asyncio.wait(pending, return_when=asyncio.FIRST_COMPLETED)
                for future in done:
                    try:
                        candidate = future.result()
                    except BaseException as exc:
                        errors.append(exc)
                        stop = True
                        continue
                    completed.append(candidate)
                    verdict = candidate.verdict
                    if verdict is not None:
                        if verdict.passed and not full_batch:
                            stop = True
                        if best is None or (verdict.failed, -candidate.index if refine else candidate.index) < (
                            best.verdict.failed, -best.index if refine else best.index
                        ):
                            best = candidate
            if errors:
                raise errors[0]
        finally:
            # Also covers caller cancellation/deadline: no detached tasks survive.
            for future in pending:
                future.cancel()
            await asyncio.gather(*pending, return_exceptions=True)
        passing = [c for c in completed if c.verdict and c.verdict.passed]
        return min(passing, key=lambda c: c.index) if passing else None

    def result(candidate: Optional[Candidate], rung: str, note: str) -> SolveResult:
        verdict = candidate.verdict if candidate else None
        return SolveResult(
            candidate.solution if candidate else None,
            verdict.passed if verdict else None,
            rung, budget.spent, len(journal) - started, verdict, note,
            list(journal[started:]),
        )

    if not budget.can_afford():
        return result(None, "none", "budget exhausted before any generation")
    if verify is None:
        candidate = await attempt(generate, None)
        return result(candidate, "greedy", "no verifier/tests supplied — returned a single un-verified candidate")

    if force_rung in ("best-of-k", "fusion"):
        gen = fusion_generate if force_rung == "fusion" else generate
        count = fusion_k if force_rung == "fusion" else k
        if count <= 0:
            return result(None, "none", "budget exhausted before any generation")
        winner = await batch(gen, count)
        return result(winner or best, force_rung, f"forced {force_rung} (test)")

    winner = await batch(generate, 1)
    if winner or force_rung == "greedy":
        return result(winner or best, force_rung or "greedy", "solved 1-shot" if winner else "forced greedy (test)")

    if force_rung is None:
        winner = await batch(generate, max(0, k - 1))
        if winner:
            return result(winner, "best-of-k", f"solved by best-of-{k}")

    for depth in range(tree_depth):
        if not budget.can_afford():
            break
        winner = await batch(generate, 1, refine=True)
        if winner:
            return result(winner, "tree-search", f"solved by refine@{depth + 1}")
    if force_rung == "tree-search":
        return result(best, "tree-search", "forced tree-search (test) — no refinement passed")

    if fusion_generate is not None and budget.can_afford():
        winner = await batch(fusion_generate, fusion_k)
        if winner:
            return result(winner, "fusion", f"solved by fusion (k={fusion_k})")
    return result(
        best, "best-partial",
        f"no candidate passed within budget; best partial has {best.verdict.failed}/{best.verdict.total} failing",
    )
