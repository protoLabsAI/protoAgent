# 0076 — Managed git for `acp` delegates: framework-owned branch/commit/push/PR

- Status: Proposed
- Date: 2026-07-05
- Builds on: ADR 0024 (spawn CLI coding agents over ACP — superseded; ACP lives on as
  the `acp` delegate), ADR 0025 (unified delegate registry — `delegate_to`), ADR 0033
  (pluggable agent runtime, ACP executor), ADR 0064 (`coder` execution-grounded
  code-solve — the board loop this hardens underneath).
- Inspired by: `protoLabsAI/protoMaker` (source-verified 2026-07-05 — a working
  autonomous dev team whose git lifecycle is framework-owned); Aider's program-owned
  commit model; GitHub Copilot coding agent's harness-mediated push.
- Plan: [`docs/plans/coding-agent-deterministic-git.md`](../plans/coding-agent-deterministic-git.md)
  (due-diligence record + phasing).

## Context

An `acp` delegate (ADR 0025) hands a coding task to a CLI coder (`proto --acp`,
claude-agent-acp, …) confined to a `workdir`, and today the **coder owns the whole git
lifecycle**: it picks its own branch name, commits, pushes, and opens the PR — the guide
even instructs it to (`docs/guides/coding-agents.md` §container, §parallel-builds).

Operating a real multi-coder team on this (roxy-protoContent: a coordinator fanning
issues across a 3-coder worktree pool) produced a stable failure taxonomy, and **every
failure traces to the LLM owning a deterministic step**:

- **Branch collisions / pool deadlock.** Linked worktrees share one `.git`; git refuses
  the same branch checked out twice. Coders inventing bare shared names deadlock the pool.
- **"Edited but never pushed."** The coder edits, reports "ready," never runs
  `git push`/`gh pr create`. Industry-wide pattern: Copilot's raw-`git push` false-success
  completions (community discussion #163356), OpenHands' live refactor away from
  agent-controlled push (OpenHands #9999).
- **Duplicate PRs.** One item fanned to several coders → 2–4 duplicate PRs. The #1
  empirical rejection cause for agent PRs across vendors (23% — arXiv 2601.15195).
- **Stray files** (`.proto/` scratch staged via `git add -A`) and **inconsistent branch
  names** (`fix/…`, `issue/…`, bare `399-1-…` coexisting).

Prompt-level rules (SOUL directives, a fork-local branch-prefix injection) reduce but do
not eliminate any of these — they are intent without enforcement.

Industry convergence is unambiguous: every shipped product (Copilot coding agent, Devin,
Cursor background agents, Jules, Claude Code, Aider) keeps **anything that touches the
remote or the PR** in deterministic harness code; several mediate or forbid model-run
push outright. protoMaker demonstrates the full pattern in production, and its source
(verified mechanism-by-mechanism, citations in the plan doc) is the port reference.

## Decision

Add an opt-in **managed-git mode to the `acp` delegate**: the coder edits files and runs
tests; a deterministic harness owns branch, commit, rebase, push, and PR.

**D1 — Surface: `delegate_to`/`AcpAdapter`, not a revived `code_with`.** New acp
delegate fields (via `FieldSpec` + `Delegate` + `AcpAdapter.parse()`, mirroring
`timeout_s`): `manage_git: bool = false` (default off — existing delegates unchanged),
`base_branch: str = "main"`, `branch_prefix: str = ""` (empty ⇒ delegate name).
`delegate_to` gains an optional runtime arg `item_id`.

**D2 — Deterministic branch identity, no LLM naming.** `item_id` defaults to
`sha1(query)[:12]`; branch = `<branch_prefix>/<slug(query,50)>-<last7(item_id)>`,
validated as a git ref. Hash-default means fanning the *same task text* to N coders
converges on one claim (see D5) instead of N branches. protoMaker's caged-LLM namer is
deliberately not ported — its own validation doesn't enforce the ID suffix; uniqueness
was always the deterministic path.

**D3 — Pre-run setup (harness, in the delegate's `workdir`):** fetch the base, then
`git checkout -B <branch> origin/<base>` (never HEAD); a reused branch with 0
commits ahead of base is hard-reset to `origin/<base>`; scratch dirs (`.proto/`, …)
seeded into `.git/info/exclude`; committer identity injected (env + local config —
containers often have none). The ACP prompt gains an edit-only directive ("do NOT
branch/commit/push or open a PR").

**D4 — Post-run lifecycle, idempotent to partial git.** Edit-only is an *instruction*,
not an assumption — protoMaker's harness is idempotent precisely because coders touch
git anyway. Sequence:

1. **Isolation guard**: `HEAD == base` ⇒ refuse and return a distinct
   `stranded_on_base` result the caller must surface as failure (work left recoverable
   in the worktree; no completion theater).
2. **Secret/scratch scan** on the diff ⇒ block commit on findings (harness-side commit
   is the only reliable interception point; agents leak secrets at ~2× human rate).
3. **Stage** `git add -A` (exclusion is structural: `.gitignore` + `info/exclude` — a
   pathspec denylist is *not* ported; protoMaker's is dead code, removed because it
   conflicts with tracked files under excluded dirs).
4. **Commit on the coder's behalf** — deterministic message
   (`<type>: <slug>\n\nItem ID: <item_id>`), `--no-verify` (CI is the verification
   layer). If the tree is clean, three-tier probe: unpushed local commits ⇒ adopt and
   continue; remote branch already ahead of base ⇒ coder pushed, add fixups as a new
   commit; neither ⇒ nothing to do.
5. **Rebase** on fresh `origin/<base>`; on conflict: capture conflicting files,
   `rebase --abort`, push as-is and report the conflict (merge-time friction, not a
   hard failure).
6. **Push** `--force-with-lease` with bounded backoff retry (covering shared-`.git`
   `index.lock`/`config.lock` contention across the worktree pool), then **verify**
   `ls-remote` SHA == local HEAD — "committed locally" never counts as pushed.
7. **Idempotent PR** via `tools/gh_cli.run_gh`: skip if 0 commits ahead of base;
   `gh pr list --head <branch>` pre-check (cures created-but-crashed); create with
   array args + retry; on "already exists", recover the real PR via
   `gh pr list --state all`.

Every step degrades gracefully and accumulates errors into a structured result
(`{pr_url, branch, pushed_sha, rebase_conflicts?, stranded_on_base?, errors}`) — a
commit survives a failed push; a push survives a failed PR create.

**D5 — Single-claim registry.** A module-global in-memory map keyed on `item_id`,
check-and-set with **no `await` between check and set** — atomic because `delegate_to`
and `AcpAdapter.dispatch` are async coroutines on one event loop (LangGraph ToolNode
gathers a turn's async tools; `background=True` is `create_task` on the same loop). The
claim is taken **inside the dispatch coroutine, before the background semaphore**, so
foreground and background fan-out share one registry; a duplicate in-flight `item_id`
returns the existing run/PR instead of dispatching; the claim releases in a `finally`.
Invariant: the path stays `async def` — if ever made sync/threaded it needs a real lock.

**D6 — Placement.** The harness is `plugins/coding_agent/git_harness.py` (plugin-local,
beside the ACP client library, portable to forks), built on `tools/shell.run_command`
(async, structured errors, process-group kill) and `tools/gh_cli.run_gh` (token
injection). `AcpAdapter.dispatch` wraps the prompt with setup/lifecycle when
`manage_git`.

**Non-goals (consciously not ported from protoMaker):** epic-branch orchestration,
prettier/format passes, changeset + lockfile automation, auto-merge (merge is owned by
the review gate / auto-merger on our side), LLM branch naming. Deferred to a later
phase: hot-file overlap defer, foundation-merged dependency gating, PID worktree locks
with push-before-remove reaping.

## Consequences

- The five roxy failure modes get **enforcement instead of prompts**: collisions
  (deterministic unique suffix), never-pushed (harness push + remote-SHA verify),
  duplicates (claim registry + PR pre-flight), strays (structural exclusion + scan),
  naming drift (minted names).
- `manage_git: false` remains the default — existing delegates and non-worktree setups
  (coder owns a whole disposable checkout) are untouched.
- The fork-local branch-prefix prompt injection and the SOUL-level git rules on roxy
  become removable; intent stays documented, enforcement moves here.
- `docs/guides/coding-agents.md` must be rewritten where it instructs the coder to do
  git (§container, §parallel-builds) and modernized off the retired `code_with` YAML.
- New test surface: a real-`git`-in-`tmp_path` repo fixture (none exists yet; matches
  `tests/test_shell.py`'s real-subprocess style) with a bare "origin"; `run_gh`
  monkeypatched for the PR layer; cases for coder-did-nothing / coder-committed /
  coder-pushed / coder-committed-on-base; fan-out dedup (N identical `delegate_to`
  coroutines ⇒ one dispatch).
- Risk: the harness commits whatever the coder left in the tree — mitigated by the
  scan step (D4.2) and structural exclusions; residual risk is accepted as strictly
  better than coder-owned `git add -A`.

## Alternatives considered

- **Keep prompt-level rules (status quo).** Rejected: measured to fail (roxy run
  logs; industry postmortems). Intent without enforcement.
- **Revive `code_with` with a config section.** Rejected: ADR 0025 unified dispatch
  under `delegate_to`; a parallel tool would re-fragment the registry and the panel.
- **Pathspec staging denylist.** Rejected: protoMaker tried and removed it (conflicts
  with tracked files under excluded dirs); its current `git add -A` + structural
  exclusion is the proven shape.
- **Caged-LLM branch naming with validation.** Rejected: adds a model call and a
  failure mode for zero benefit over deterministic minting; protoMaker's regex doesn't
  even enforce the uniqueness suffix.
- **Server-side claim store (DB/file-locked).** Rejected for v1: the event-loop-atomic
  in-memory registry covers one instance, which is the actual topology (claims are
  per-coordinator); cross-instance dedup is the PR pre-flight's job. Revisit if
  multi-instance dispatch of one board becomes real.

## Phasing

Per the plan doc: P1 branch minting + pre-run setup → P2 post-run lifecycle → P3 claim
registry + `item_id` + PR pre-flight → P4 roxy wiring, band-aid removal, guide rewrite →
P5 dogfood (399.5b, 394.3) → P6 (later) overlap defer, foundation gating, worktree locks.
