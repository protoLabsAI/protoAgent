"""The ``coder`` subagent face (ADR 0064).

A subagent is a *prompted* LLM worker (ADR 0020) — it cannot run the deterministic
search ladder itself. So this face is deliberately thin: its one job is to call the
``coder_solve`` tool (which runs the ladder) and relay the verified result. It
exists for ergonomics + the Subagents panel (progressive disclosure: the lead sees
the verdict, not the rollouts), while the real work lives in the tool/library that
the board also calls directly.
"""

from __future__ import annotations


def build_coder_subagent():
    """The ``SubagentConfig`` for the lead-agent ``task()`` face. Returns None if the
    core subagent type is unavailable (keeps this import-light for unit tests)."""
    try:
        from graph.subagents.config import SubagentConfig
    except Exception:  # noqa: BLE001 — running without the graph package (unit tests)
        return None

    return SubagentConfig(
        name="coder",
        description=(
            "Solves a VERIFIABLE coding task by execution-grounded search and returns a "
            "TEST-VERIFIED solution (or the best partial with the failing cases named). "
            "Use when you can supply tests/acceptance for the subtask — 'implement X, "
            "here are the tests'. Not for open-ended exploration; it shines when there's "
            "an oracle to run."
        ),
        system_prompt=(
            "You are `coder`. You solve a single verifiable coding task by calling the "
            "`coder_solve` tool exactly once, passing the task and the caller-supplied "
            "tests. `coder_solve` runs an execution-grounded ladder (greedy → best-of-k "
            "→ tree-search) and returns a test-verified solution or the best partial with "
            "named failing cases.\n\n"
            "Do NOT try to write or fix the code yourself in prose — that re-introduces "
            "the unverified-plausible-code failure mode the tool exists to avoid. Call "
            "`coder_solve`, then relay its result faithfully: the solution, whether it "
            "passed, which cases failed (if any), and the generations spent."
        ),
        tools=["coder_solve"],
        max_turns=4,
        # One-off solves over caller-specific code aren't reusable distilled skills.
        allow_skill_emission=False,
    )
