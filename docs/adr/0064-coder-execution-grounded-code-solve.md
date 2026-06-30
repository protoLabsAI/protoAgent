# 0064 — `coder`: execution-grounded code-solve (the board's verifier-grounded coder)

- Status: Accepted
- Date: 2026-06-29
- Builds on: ADR 0002 (reusable subagent workflows), ADR 0020 (subagents via the
  `task` tool), ADR 0024 (spawn CLI coding agents over ACP), ADR 0025 (unified
  delegate registry — `delegate_to`), ADR 0055 (multi-team orchestration —
  portfolio → Lead Engineer team tiers).
- Composes (external plugins): `projectBoard-plugin` (beads board + ACP spawn loop —
  the Lead Engineer team's coding engine), the `leadEngineer` bundle (team tier),
  `pm-stack` / `portfolio-plugin` (portfolio tier).
- Inspired by: lab prototype [`experiments/code-tree-search/`](https://github.com/protoLabsAI/lab/tree/main/experiments/code-tree-search);
  `protolabs/fusion` (self-MoA, protoContent #351/#365); MARTI / MARS² (learned
  multi-agent tree search for code).
- Issue: protoAgent #1440.

## Context

protoAgent ships a real coding executor — the **`acp` delegate** (ADR 0024/0025): a
CLI coding agent (`proto --acp`, claude-agent-acp, …) with its own edit/verify loop,
driven over ACP, confined to a `workdir`. The **`projectBoard-plugin` spawn loop**
(the Lead Engineer team's engine, ADR 0055) is its primary autonomous caller: it
pulls the top `ready` feature → creates a disposable `git worktree` off
`origin/<base>` → **dispatches one `acp` coder scoped to it** → commits/pushes →
opens a PR → `in_review`; a merge webhook (or `merge_poll`) drives `done`.

That loop already has an escalation ladder and already gates on an acceptance
contract — but on the wrong axis, and with no execution oracle:

- **Escalation is by model tier, not by search.** With a `coders` map of >1 delegate,
  a *capability failure* climbs `fast → smart → reasoning` and blocks at the top.
  That throws a bigger brain at the problem; it never searches harder on a fixed
  model.
- **The gating signal is coarse and verifier-blind.** Escalation fires on
  **"no diff / timeout"** — *did the agent do anything?* — not **"did the result
  work?"** The only correctness gate is PR review / CI after the fact. There is no
  *run-the-tests-and-select-the-passing-candidate* step anywhere in the loop.
- **The oracle is already sitting there, unused.** The board's **Ready gate already
  requires a spec + EARS acceptance criteria + explicit `files_to_modify`** before a
  feature is `ready`. Today those acceptance criteria are *prompt text*. They are a
  latent, per-feature **verifier** the loop never executes.

Two further facts make this the right moment:

- **LLM-judged code ceilings.** A judge-of-code rewards plausible code and can't
  catch subtle wrongness; only *running* it discriminates. The lab hit this exactly —
  a strong model "aced" an LLM-judged coding suite while real execution spread the
  scores. The board inherits this ceiling: its terminal signal is "PR opened," not
  "tests pass."
- **`protolabs/fusion` is unrealized for code.** fusion (self-MoA) is a strong
  *generator* whose selector is a **blind judge** and which **can't tool-call**. For
  code-with-tests you never needed a judge — you can run the candidates. fusion's two
  limits dissolve precisely in this setting.

## Decision

Add **`coder`**: an **execution-grounded code-solve orchestrator** that turns a
*verifiable* coding task into a **test-verified** solution by a difficulty-gated
search ladder, with fusion as the optional top rung. It ships as a git-URL plugin and
**composes** the `delegates` registry and `code_exec` — it does not reimplement the
ACP/A2A spawn primitive (same discipline as `projectBoard-plugin`).

`coder` is the **missing execution-verification rung in the Lead Engineer board
loop** — not a new tier, and explicitly **not** a portfolio-manager concern (the PM
runs no board; it only sees the better outcome + cost in `portfolio_rollup`).

### Shape: an orchestrator, exposed two ways

The escalation ladder — spawn N candidates, *run their tests*, select the passing
one, refine on the failures — is **deterministic control flow over delegate calls +
a verifier**, not something a single prompted LLM does. So `coder`'s core is a
**`solve()` orchestrator** (a library in the plugin). It is reached as a **tool**,
not as the subagent itself: a `SubagentConfig` is a *prompted LLM loop* (ADR 0020)
and cannot run deterministic gating — but a LangChain tool runs arbitrary Python, so
the ladder lives in a **`coder_solve` tool** that wraps `solve()`. That one engine is
exposed three (composing) ways:

1. **Tool** (`coder_solve`) — the deterministic surface. The lead agent calls it
   directly; it runs the ladder and returns the verified result. This *is* the
   orchestrator's public face.
2. **Subagent face** (ADR 0002/0020) — a **thin prompted wrapper** registered in
   `SUBAGENT_REGISTRY` whose only job is to call `coder_solve` once and relay the
   result. It exists for ergonomics + the **Subagents panel** + progressive
   disclosure (the lead sees the verdict, not the rollouts) — it does *not*
   re-implement the search in prose.
3. **Board face** — `projectBoard-plugin`'s loop calls `solve()` (the library)
   directly as its **per-feature coder**, replacing the bare single
   `delegate_to(acp)` shot. This is the seam that makes the board verifier-grounded
   (board-side wiring lands in `projectBoard-plugin`; see "The board seam").

All three share the same orchestrator, verifier contract, and cost accounting.

### The ladder — gated on tests, not on "did anything happen"

Each rung fires **only when the cheaper one fails its tests**:

```
1. greedy        1-shot (acp coder / protolabs/smart)         cheap; solves most
2. best-of-k     k candidates → run tests → execution-select  headroom recovery
3. tree-search   refine on the *failing* tests, bounded depth  grounded fix loop
4. fusion        protolabs/fusion candidates → execute-select  hardest; richer + oracle-picked
```

Rung 4 is the **fusion combination**: fusion is the *generator*, execution is the
*selector* — "fusion proposes, tests dispose." This dissolves fusion's two limits (no
tool-calling needed at the rung; the blind judge is replaced by the oracle) and pays
fusion's ~3× cost **only** on genuinely hard, verifiable problems.

### The verifier — the one hard dependency

A rung's gate is **test pass/fail**, resolved in priority order:

1. **Caller-supplied tests** (the `task()` subagent path) — run in a sandboxed
   `code_exec` runner.
2. **Board acceptance** (the board path) — the feature's **EARS acceptance criteria
   are compiled to runnable tests** and executed in the feature's existing
   `git worktree` (the `acp` delegate's real sandbox, ADR 0024). This is the latent
   oracle the Ready gate already collects.

**No oracle ⇒ no grounding.** When neither is available, `coder` **degrades to
greedy** (1-shot) — *not* a silent best-of-k, because without execution the only
selector left is an LLM judge, which is the ceiling this ADR exists to escape. A
best-of-k-with-judge fallback is offered only behind an explicit opt-in, logged as
weaker. Documented scope line: **`coder` shines on *verifiable* coding**; on
open-ended work with no acceptance contract it is a thin wrapper over today's single
ACP shot.

### The board seam (wiring lands in `projectBoard-plugin`)

The board loop's per-feature step changes from:

```
ready feature → worktree → delegate_to(acp coder) → (no diff? climb model tier) → push → PR
```

to:

```
ready feature (+ EARS acceptance → tests) → worktree → coder.solve():
    greedy → run tests → fail? best-of-k into k worktrees → execute-select
           → fail? tree-search refine on failing tests
           → fail? fusion candidates + execute
  → push the test-passing candidate → PR   (PR review now stands on green tests)
```

The two escalation axes **compose, they don't conflict**: `coder` searches *within* a
model tier (rungs 1–4); the board's existing `coders`-map ladder escalates the *tier*
when search stalls (a capability failure at the top rung climbs `smart → reasoning`).
The board keeps owning board projection, worktree lifecycle, retry/backoff, and the
PR/merge edges; `coder` owns the execution-grounded solve *inside* the worktree.

### New vs reused

Almost all of the substrate already exists:

| Ladder need | Reuses (already built) |
|---|---|
| greedy / candidate generation | `acp` delegate dispatch (ADR 0024/0025) |
| best-of-k isolation | disposable `git worktree` + `forget_session`/`evict_client` per-attempt teardown (`coding_agent/__init__.py`); board `max_concurrent` parallel worktrees |
| fusion rung | `protolabs/fusion` is an existing **`openai` delegate** — drops into the candidate set |
| verifier substrate | `code_exec` runner (caller tests) **or** the worktree/acp sandbox (board) |
| cost surfacing | `portfolio_rollup` (PM tier) + per-feature board metrics |

**Genuinely new work:** (1) compile EARS acceptance → runnable tests + read pass/fail;
(2) **execution-select** across best-of-k candidates; (3) **tree-search refine** —
feed failing-test output back as the next attempt's prompt, bounded depth; (4) the
fusion-rung wiring + the `solve()` orchestrator and its two faces.

### Cost & budget (cost-v1)

Every `solve()` returns **gens-spent** alongside the verdict, and runs under a
**generation budget** (a hard cap; the fusion rung is budget-gated on top of being
test-gated). A partial result names the **failing cases** rather than returning a
plausible-but-wrong solution. Board features carry gens-spent through to
`portfolio_rollup` so the portfolio tier reasons about cost without raw reads.

## Consequences

- **The board stops shipping plausible code.** Verification moves from "PR opened →
  reviewed later" to "tests passed → then reviewed for design/intent." PR review is
  grounded on green tests instead of guessing correctness from a diff.
- **fusion finally earns its keep for code** — as a *generator* selected by an
  oracle, at ~3× cost paid only on the hardest verifiable problems.
- **Headroom recovery on a fixed model.** The lab numbers: execution-grounded search
  lifts gemma4-12B **5/6 → 6/6**; a ceiling model (Ornith-35B) stays 6/6 (pure cost) —
  so the ladder's gating is doing real work, and the budget cap keeps the no-win case
  cheap.
- **No new tier, no new dispatch primitive.** `coder` composes `delegates` +
  `code_exec`; the board composes `coder`; the PM is untouched. The layering contract
  holds.
- **Honest degrade.** Without an acceptance oracle, `coder` is greedy-only by default
  and says so — it does not fake grounding with an LLM judge.

## Alternatives considered

- **Just add more model tiers to the board's existing ladder.** Rejected — it
  escalates the *wrong axis* (bigger brain, not harder grounded search) and never
  inserts an execution oracle, so the verifier-blind ceiling remains.
- **`coder` as a pure prompted `task()` subagent (no orchestrator).** Rejected — the
  ladder (spawn-k, run-tests, select, refine) is deterministic control flow; a
  prompted LLM "deciding" to do best-of-k is exactly the un-grounded behavior we're
  removing. Keep the search in code; expose a subagent face over it.
- **Build it into `projectBoard-plugin` directly.** Rejected — `projectBoard`
  deliberately *composes* `delegates` and "does not reimplement it." `coder` is its
  own composable solver so the `task()` subagent face (ad-hoc lead use) and the board
  face share one engine, and other hosts can consume it without the board.
- **best-of-k + LLM judge when no tests exist (always on).** Rejected as a default —
  it re-introduces the judge-of-code ceiling. Offered only as an explicit, logged-as-
  weaker opt-in; the honest default with no oracle is greedy.

## Acceptance (from #1440)

- Plugin installs from a git URL and registers a `coder` subagent (appears in the
  Subagents panel).
- Given a task + tests, `coder` returns a solution passing all tests, or its best
  partial **with the failing cases named**, within a generation budget.
- Escalation is gated by test pass/fail; the **fusion rung only fires after** cheaper
  rungs fail.
- Degrades to greedy (1-shot) when no verifier/tests are available; the documented
  scope line says it shines on *verifiable* coding.
- The board loop (`projectBoard-plugin`) can call `coder.solve()` as its per-feature
  coder, with the Ready-gate EARS acceptance compiled to the verifier.
- Ships with one worked example + a protoAgent eval task; gens-spent surfaced per the
  cost-v1 ethos.

## Phasing

- **P1 — `coder` solver + subagent face.** `solve()` orchestrator, rungs 1–3 over the
  `acp`/`openai` delegates, `code_exec` verifier for caller-supplied tests, budget +
  gens-spent, the `SUBAGENT_REGISTRY` entry, worked example + eval task.
- **P2 — board seam.** `projectBoard-plugin` compiles EARS acceptance → tests and
  dispatches `coder.solve()` per feature in the worktree; gens-spent on the feature →
  `portfolio_rollup`. Compose with the existing model-tier `coders` ladder.
- **P3 — fusion rung.** Wire `protolabs/fusion` candidate generation + execute-select
  as rung 4, budget-gated; measure against the lab numbers.
