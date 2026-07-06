# Program: the Codified Delivery Loop (two sprints)

**Status:** scoped by due diligence 2026-07-06, ready to execute.
**Goal:** encode the working delivery loop — *plan → due-diligence → ADR → build → review →
settle* — as first-class mechanisms and gates across the three tiers, so leadEngineer /
roxy-class agents run the same loop we run by hand.
**Tiers (ADR 0055):** protoAgent core (mechanism) → projectBoard-plugin / leadEngineer
(lifecycle policy) → portfolio-manager-stack (meta/observer).
**Prior art this builds on:** ADR 0076 (managed git, shipped #1845/#1846/#1847), ADR 0064
(execution-grounded solve), ADR 0002 (workflow recipes), ADR 0052/0060 (skills),
`docs/plans/coding-agent-deterministic-git.md` (Phases 4–5 folded in here as M4).

---

## 0. Due-diligence summary (what the surfaces can and can't do today)

Three investigations (2026-07-05/06): the shepherd framework + protoAgent overlap map, the
workflows/skills/subagents engine survey, and the projectBoard lifecycle map. Verdict on
shepherd: don't adopt (11-day-old alpha, bus factor 1, meta features unshipped, macOS-first,
claude-sdk-bound); codify our loop on our substrate (boards + git/GitHub), steal the
fork-per-candidate idea. Key implementation facts:

**protoAgent engines** (`plugins/workflows/`, `graph/subagents/`, skills):
- Recipes are YAML step-DAGs; parallel fan-out = N static steps; steps run subagents via
  `sdk.run_subagent`; **all inter-stage output is prose** — no typed findings, no
  data-driven per-item fan-out (deferred by ADR 0002). `deep-research.yaml` already has the
  gather ∥ dissent → antagonist ∥ verify → synthesize shape.
- Subagent roles are allowlist-scoped (`graph/subagents/config.py`); the adversarial
  researcher/antagonist/verifier/synthesizer split exists; **no role has git/gh/fs tools**.
- The craft `/code-review` skill is a fixed 2-axis prose brief — far short of an N-angle
  adversarial review. Skills can gather inputs then call the `run_workflow` tool.
- Diff access: `github_get_commit_diff` exists; **no PR-diff tool** (`github_get_pr` returns
  metadata + file list only).
- No findings schema anywhere; reports render via the ADR 0070 report card + ADR 0062
  DocViewer.

**projectBoard-plugin** (board = projection over beads; 6 states; loop = only mover):
- Ready gate = spec + EARS acceptance + files_to_modify (`store.mark_ready`); **`design` is
  already a first-class field** but never gated; `difficulty` ∈ {small, medium, large,
  architectural} already classifies features (`diff:` label).
- Foundation merge-gating, dep gates (`dep_gate: merge|review`), and hot-file overlap
  serialization **already exist at the board tier** — ADR 0076's deferred P6 items are DONE
  here; do not rebuild them in core.
- Between PR-open and merge there is **no blocking internal review** — `review_dispatch` is
  a fire-and-forget advisory a2a ping. The CI-feedback bounce pattern
  (`_ci_feedback`/`ci_block` + `ci_fix_max`) is the ready-made template for a findings-driven
  review gate.
- The decompose pipeline (decompose + antagonist subagents + per-epic human approval)
  already authors specs/ADRs — the missing piece is the *gate*, not the authoring.
- **Landmine:** `worktree.dispatch_coder` calls `ADAPTERS["acp"].dispatch` directly and its
  `dataclasses.replace(coder, workdir=…)` **preserves `manage_git`** — a `manage_git: true`
  delegate would double-run the whole git lifecycle against the board's own
  worktree/branch/PR (duplicate branches, rejected PRs, branch fights in the shared
  `.git`). The board never passes through `registry.dispatch(raw=True)`, so the coder-ladder
  bypass doesn't protect it.
- ADR 0064's board face (`solve()` per feature) is **not yet wired** — the loop still does
  the bare single acp shot. Out of scope here; sequenced note in M5.

---

## Sprint 1 — mechanisms (protoAgent core) + landmine removal

### M0 — Board × managed-git safety fix  `projectBoard-plugin`  [S]  ⚠ do first
The board force-disables managed git on its scoped dispatch: `manage_git=False` in the
`dataclasses.replace` at `worktree.py:224` (decision: **the board keeps owning git** — it
already implements the queue-level gates 0076 deferred; revisit handing the lifecycle to the
harness only if the board's git code becomes a burden). Regression test: a `manage_git: true`
delegate dispatched by the loop produces exactly one branch/PR.
**Blocks:** M4, and any roxy/leadEngineer host that flips `manage_git` on.
**Accept:** test proves single-PR; released as a patch tag.

### M1 — Adversarial review workflow  `protoAgent`  [the core build, M overall]
The N-angle finder → dedup → verify → synthesize pipeline as a first-class recipe.
- **F1.1** `github_pr_diff(number, repo)` tool (`gh pr diff` via `run_gh`) — [S]
- **F1.2** Findings convention: one module (`graph/review/findings.py` or similar) defining
  the JSON-in-prose schema `[{file, line, severity, category, claim, evidence, verdict?}]`
  + a tolerant parser + a Markdown renderer. Engine stays string-based; this is the
  contract both the recipe prompts and consumers (board gate, console) share. — [M]
- **F1.3** New subagent roles: `review-finder` (diff-reading allowlist:
  `github_pr_diff`/`github_get_commit_diff`/`github_read_file`; prompt parameterized by
  angle: correctness / removed-behavior / cross-file / conventions) and `review-synthesizer`
  (dedup + rank + render findings). Reuse `verifier` for the verify pass. — [M]
- **F1.4** `code-review.yaml` recipe: 4 static finder steps ∥ → dedup/synthesize step →
  **single verify step over the merged list** (the honest compromise: per-finding verify
  fan-out needs a data-driven engine primitive — deferred, see Parked) → final findings
  block. — [M]
- **F1.5** Upgrade the craft `/code-review` skill: gather the diff/PR ref, call
  `run_workflow("code-review", …)`, present findings (replaces the 2-axis brief). — [S]
- **ADR** for the workflow + findings convention (cite this plan). — [S]
**Accept:** `/code-review <PR#>` on a real PR yields deduped, verified, parseable findings;
recipe callable headlessly via `run_workflow` (that's what the board gate consumes in M5).

### M2 — Due-diligence workflow  `protoAgent`  [S–M]
- **F2.1** `codebase-mapper` subagent role (fs/github read allowlist) — the missing gather
  angle. — [M]
- **F2.2** `due-diligence.yaml`: re-author deep-research with codebase-map ∥
  external-research → antagonist ∥ claims-verify → cited synthesis with an explicit
  adopt/build/defer verdict contract. — [S]
- **F2.3** `/due-diligence` user-facing skill: scope the question, then `run_workflow`. — [S]
**Accept:** a DD run on a named library/approach returns a cited verdict document; callable
headlessly (M6 consumes it).

### M3 — ADR/plan authoring skill  `protoAgent`  [S]
Craft-style `SKILL.md` encoding the house conventions: MADR shape, Builds-on lineage,
`docs/adr/index.md` row, `scripts/gen_docs_nav.py` regen, VitePress gotchas (no wrapped code
spans), plan-doc conventions in `docs/plans/`. The decompose antagonist already checks "cites
ADRs" — this skill is what makes agent-authored ADRs meet the bar.
**Accept:** an agent-authored test ADR passes docs build + nav test first try.

### M4 — roxy managed-git wiring + dogfood  `roxy fork` (local)  [M]  (= 0076 Phases 4–5)
After M0 lands in the board version roxy runs: flip the `proto-1..3` pool delegates to
`manage_git: true`, remove the fork-local branch-prefix injection, simplify SOUL git rules,
rebuild/redeploy; re-run 399.5b (LabBoard cell-map dedup fix) and 394.3 (VRAM chart) through
the deterministic path.
**Accept:** N parallel items → N clean single PRs, verified pushes, no strays; the two
outstanding items merged.

---

## Sprint 2 — lifecycle gates (board tier) + roll-out

### M5 — Blocking REVIEW gate  `projectBoard-plugin`  [M–L]
Turn `review_dispatch` from advisory into a gate, mirroring the CI-bounce machinery:
- After `open_review`, invoke the M1 review workflow (via `sdk`/subagent on the host, with
  the a2a reviewer as a config alternative); parse findings (F1.2 schema).
- New sub-states as labels: `review-pending` / `changes-requested`; findings stored via
  `_comment` + injected into re-dispatch prompts exactly like `ci_block`; bounded by
  `review_fix_max` (mirror `ci_fix_max`); exhaustion → `flag_blocked`, never silent merge.
- Wire into `_drive` (post-`open_review`) and `_reconcile_prs` (gate the merge edge);
  config: `review_gate: bool` (default false), `review_workflow`, `review_fix_max`.
- Sequencing note: when ADR 0064's board face lands later, review attaches *after*
  test-passing candidate selection — design the gate call-site so that reordering is a
  one-line move.
**Accept:** with `review_gate: true`, a PR with seeded defects bounces with findings in the
coder's retry prompt and merges only once findings are clean; `test_loop.py` coverage for
bounce/budget/exhaustion.

### M6 — DESIGN gate  `projectBoard-plugin`  [S–M]
- Extend `mark_ready` (`store.py:218-232`): `difficulty ∈ {large, architectural}` requires a
  non-empty `design` field (exists) referencing an ADR; `BoardError` otherwise. — [S]
- Optional `designing` pre-ready state (label) + loop/skill hook that runs the M2 DD
  workflow and writes `design` + the ADR before `mark_ready`. — [M]
- Update `board_mark_ready` tool docs + decompose SKILL.md step 5.
**Accept:** an architectural feature cannot reach `ready` without design+ADR; small features
unaffected; `test_store.py` coverage.

### M7 — Release + bundle roll-out  `projectBoard-plugin`, `leadEngineer`, `pm-stack`  [S]
Board releases (M0 patch early; M5/M6 minor later); leadEngineer + pm-stack pin bumps
through `verify-bundle`; bundle `config.project_board` gains the recommended gate defaults
(`review_gate: true` for leadEngineer; pm-stack ships teams with it). `verified_against`
bump if core moved.
**Accept:** verify-bundle green on both bundles; a freshly spawned team has the gates on.

### M8 — PM-tier flywheel ADR  `portfolio-plugin` (design only)  [M, stretch]
Design-only ADR: the PM observes review findings + bounce rates + ladder escalations across
teams' boards (rollup surface) and turns them into dispatch policy / process advice —
ADR 0006's flywheel extended past "advise", grounded in the M5 findings data that will now
exist. No implementation this sprint.
**Accept:** ADR reviewed/accepted; implementation scoped for a later sprint.

---

## Dependency graph

```
M0 ──► M4 (roxy)                     M1 ──► M5 ──► M7 ──► (M8 design consumes M5 data)
M2 ──► M6 ──► M7                     M3 (independent; supports M2/M6 authoring quality)
```
Sprint 1: M0, M1, M2, M3, M4.  Sprint 2: M5, M6, M7, M8(stretch).
M0 is a half-day and de-risks everything; start there.

## Parked (explicitly out of scope)

- **Workflow-engine data-driven fan-out** (per-finding verify, dynamic N): revisit only if
  M1's single-verify pass demonstrably misses; it's an ADR 0002 amendment, not a patch.
- **Typed inter-stage outputs in the engine**: JSON-in-prose + shared parser (F1.2) is the
  v1 contract; engine-level schemas are a later hardening.
- **ADR 0064 board face** (`solve()` per feature): separate track; M5 only leaves it a
  clean call-site.
- **Shepherd adoption**: re-evaluate ~2 quarters (shepherd2 maturity, Linux enforcement,
  contributor diversity). Its fork-per-candidate idea is partially covered by the board's
  Max-Mode N-parallel dispatch already.
- **Worktree-per-candidate parallel best-of-k for the coder ladder** (coder P2): valuable,
  separate track after M0 settles git ownership.

## Fleet/context notes

- roxy (`/home/josh/dev/roxy`, local-only fork) is the dogfood host for M4; protoContent's
  external protoPatch gate stays as the outer settlement layer — M5's internal gate runs
  *before* it, cutting bounce round-trips with the remote gate.
- projectBoard working tree is at v0.25.0 while bundles pin v0.29.2 — sync the checkout
  before M0.
- This plan supersedes Phases 4–6 of `coding-agent-deterministic-git.md` (P4/P5 → M4; P6
  items proved already implemented at the board tier — see DD summary).
