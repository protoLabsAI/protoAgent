# Verifier-grounded coder (`coder_solve`)

The `coder` plugin solves a **verifiable** coding task — "implement X, here are the
tests" — by an **execution-grounded search ladder** and hands back a solution that
*actually passes the tests*, not a plausible-looking one. It's the verifier-grounded
counterpart to the [ACP coding agent](/guides/coding-agents): where a bare `acp`
dispatch is one un-checked shot, `coder` runs candidates, **runs their tests**, and
escalates only when the cheaper rung fails.

Design rationale and the board integration are in
[ADR 0064](/adr/0064-coder-execution-grounded-code-solve).

## What it contributes

- **`coder_solve` tool** — the lead agent (or a subagent) calls it with a task and
  tests; it runs the ladder and returns a test-verified result.
- **`coder` subagent** — a thin prompted face over the tool (appears in the Subagents
  panel; the lead `task()`-delegates to it and sees the verdict, not the rollouts).

Both are one engine: a deterministic `solve()` ladder. The subagent can't run the
ladder itself (it's a prompted LLM loop), so the orchestration lives in the tool.

## The ladder

Each rung fires **only when the cheaper one fails its tests**:

```
1. greedy        1-shot                                   cheap; solves most
2. best-of-k     k candidates → run tests → select         headroom recovery
3. tree-search   refine on the *failing* tests, bounded     grounded fix loop
4. fusion        richer candidates → execute-select         hardest (opt-in)
```

Rung 4 is opt-in: set `coder.fusion_delegate` to a richer generator (e.g.
`protolabs/fusion`, an `openai` delegate). It fires **only** after the cheaper rungs
fail their tests and only while budget remains, so it pays fusion's ~3× cost solely
on genuinely hard, verifiable problems — "fusion proposes, the tests dispose."

The gate is **test pass/fail**, never an LLM judge. With **no tests**, `coder`
degrades to a single un-verified candidate and says so — it shines on verifiable
work.

## Configure

`coder` **composes the [`delegates`](/guides/delegates) plugin** — it generates
candidates by dispatching to a declared delegate (an `openai` model endpoint, or an
`acp` coder). It ships **disabled** (it runs model-authored code in a subprocess —
isolation, not a true sandbox; enable for a trusted model or a hardened container).

```yaml
# langgraph-config.yaml
delegates:
  - name: smart                 # the generator coder_solve dispatches to
    type: openai
    description: Code generator.
    url: https://api.proto-labs.ai/v1
    model: protolabs/smart

plugins:
  enabled: [delegates, coder]

coder:
  delegate: smart               # REQUIRED — a declared delegate name
  budget: 6                     # hard cap on total generations across the ladder
  k: 3                          # best-of-k width (rung 2)
  tree_depth: 2                 # refine-on-failing-tests rounds (rung 3)
  test_timeout: 60.0            # per-candidate pytest timeout (seconds)
  solution_name: solution       # module candidates are written to; tests import it
  # fusion_delegate: fusion     # optional rung 4 — a richer generator (declare it in delegates)
  # fusion_k: 2                 # fusion candidates per attempt at the top rung
```

## Worked example

Ask the agent to solve a task with tests (the tests import the solution module as
`solution`):

> Use `coder_solve` to implement `add(a, b)` returning their sum. Tests:
> ```python
> from solution import add
>
> def test_add():
>     assert add(2, 3) == 5
>     assert add(-1, 1) == 0
> ```

`coder_solve` writes each candidate to `solution.py`, runs the tests under `pytest`
in a throwaway temp dir, and returns a JSON result:

```json
{
  "passed": true,
  "rung": "greedy",
  "gens_spent": 1,
  "candidates_tried": 1,
  "note": "solved 1-shot",
  "failing": [],
  "solution": "def add(a, b):\n    return a + b\n"
}
```

On a harder task the same call escalates — `rung` becomes `best-of-k` or
`tree-search`, `gens_spent` climbs (bounded by `budget`), and a result that never
passes returns `"passed": false` with the **failing cases named** rather than a
plausible-but-wrong solution.

## Cost

Every solve reports `gens_spent` and runs under a hard `budget`. The expensive rungs
fire only after cheaper ones fail their tests, so you pay for search depth only on
genuinely hard, verifiable problems.

## Eval

The suite ships an opt-in case, `coder_solve_verifiable` (category `tool`), gated on
`requires_env: [CODER_EVAL]` — set `CODER_EVAL=1` and run against an agent with
`coder` enabled and a delegate configured:

```bash
CODER_EVAL=1 python -m evals.runner --tasks coder_solve_verifiable
```
