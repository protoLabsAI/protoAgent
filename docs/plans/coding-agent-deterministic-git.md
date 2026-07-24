# Plan: deterministic, framework-owned git for ACP coder delegates

**Status:** Executed — [ADR 0076](../adr/0076-managed-git-acp-delegates.md) Accepted;
shipped #1845/#1846/#1847 (`plugins/coding_agent/git_harness.py`, `manage_git` wired
through the delegates registry).
Supersedes the 2026-07 draft that targeted `code_with`.
**Owner:** protoAgent `delegates`/`coding_agent` surface (general capability → contribute
upstream; test on roxy).
**Origin:** the roxy-protoContent dev team kept hitting coder-reliability failures.
Due-diligence on `protoLabsAI/protoMaker` (a *working* autonomous dev team) showed the fix:
**git is framework-owned, the LLM only edits files.** This plan ports that.

---

## 0. Due-diligence summary (what changed vs. the first draft)

Three tracks ran on 2026-07-05: full source verification of protoMaker
(`/home/josh/dev/protoMaker`), a map of protoAgent's current coder surface, and industry
research on git ownership in agent harnesses.

**The approach is confirmed.** Every major product (Copilot coding agent, Devin, Aider,
Cursor background agents, Jules, Claude Code) keeps remote-touching git — branch lifecycle,
push credentials, PR creation — in the harness, never the model. Aider is the pure form of
what we're building (LLM edits, program commits). OpenHands is actively refactoring *away*
from agent-controlled push after "agent said done, never pushed" incidents; Copilot mediates
push through a tool because raw `git push` from the agent caused false-success completions
(community discussion #163356). An empirical study of 33.6k agent PRs (arXiv 2601.15195)
found **duplicate PRs are the #1 rejection cause (23%)** — our single-claim registry attacks
the top documented failure mode.

**Corrections that re-target the plan:**

1. **`code_with` no longer exists.** ADR 0025 retired it; `plugins/coding_agent/` is now a
   plain ACP client *library* (no manifest, no tool). The live surface is
   **`delegate_to(target, query, background=False)`** (`plugins/delegates/__init__.py:33`)
   dispatching through **`AcpAdapter`** (`plugins/delegates/adapters.py:471-674`). The git
   lifecycle attaches there: new `Delegate` fields + `FieldSpec`s on the acp adapter, plus a
   runtime arg on `delegate_to` — **not** a resurrected `code_with` config section.
2. **The branch-prefix prompt-injection band-aid is in roxy's fork**
   (`/home/josh/dev/roxy`), not in this repo. Nothing to revert here; roxy drops it in
   Phase 4.
3. **protoMaker's staging is `git add -A` + `.gitignore`, not a pathspec denylist.** The
   `excludeFromStaging` denylist in its code is dead (param ignored) — the pathspec approach
   was removed because it conflicts with tracked files under excluded dirs. Scratch exclusion
   is done structurally: per-worktree `.git/info/exclude` entries and `git rm --cached` for
   stray tracked files. Port that, not a denylist.
4. **"Edit-only" is an instruction, not an invariant.** In protoMaker, frontend/PM agents
   are genuinely git-less but backend agents have Bash and sometimes commit; the harness is
   **idempotent to partial git** precisely because coders touch git anyway. Our harness must
   keep the same three-tier detection (see §2) rather than assume a clean tree.
5. **Skip LLM branch naming entirely.** protoMaker's caged-LLM fallback doesn't even enforce
   the ID suffix in its validation regex — uniqueness really comes from the deterministic
   path. We go deterministic-only: simpler, and the collision-proofing is the ID suffix.
6. **Research adds three hardening requirements** the draft missed:
   - **Verify the remote after push** (`git ls-remote` SHA == local HEAD) before reporting
     success — "committed locally" must never count as done.
   - **Secret scan + scratch check at harness commit time** — agents leak secrets at ~2× the
     human rate; harness-side commit is the natural (and only reliable) interception point.
   - **Retry-with-backoff on `.git` lock contention** — linked worktrees share one `.git`;
     parallel harness runs hit `index.lock`/`config.lock` races (documented Claude Code bugs
     #34645, #55724).

## 1. Why (the problems we're solving)

Today an acp delegate hands the whole job to the LLM coder: branch, commit, push, PR. Every
reliability failure on roxy traces to the LLM owning a **deterministic** step:

- **Branch collisions / deadlock.** A worktree pool shares one `.git`; git refuses the same
  branch in two worktrees. Fan-out with bare shared branch names deadlocks the pool.
- **"Edited but never pushed."** Coder reports "ready," never runs `git push`/`gh pr create`
  (394.1, 399.4/5b runs — and the industry-wide pattern above).
- **Duplicate PRs.** One item fanned to multiple coders → 2–4 duplicate PRs (399.3 ×2,
  399.5a ×4). Also the #1 empirical rejection cause across all vendors.
- **Stray files.** Coder `git add -A`'s its `.proto/` scratch into the PR.
- **Inconsistent branch names** (`fix/…`, `issue/…`, bare `399-1-…` coexisting).

Prompt-level band-aids (roxy SOUL rules, roxy-fork branch-prefix injection) keep the
*intent*; enforcement moves into the framework. roxy's entrypoint worktree pool `wt-1..N`
stays — it's the isolation substrate (documented in `docs/guides/coding-agents.md` §Parallel
builds, PR #1844).

## 2. The protoMaker pattern (verified, what to port)

All mechanisms confirmed in source at `/home/josh/dev/protoMaker`:

| Concern | Verified mechanism | File |
| --- | --- | --- |
| Branch name | `<prefix>/<slug(title,50)>-<last7(itemId)>`, minted and persisted **before** any coder runs; prefix from category map (`fix|chore|docs|feature`), conventional-commit type in the title wins over a default category | `feature-loader.ts:398-475`, persistence `:964-997` |
| Worktree branch setup | Always `git fetch origin <base>` first, branch off `origin/<base>` **never HEAD** (`worktree add -B <branch> <path> origin/<base>`); deterministic path; orphan-dir pre-clean; **stale reused branch with 0 unique commits → `reset --hard origin/<base>`**; partial-dir cleanup on failure | `auto-mode-service.ts:3137-3376` |
| Isolation guard | Before commit: `rev-parse --abbrev-ref HEAD`; if `== base` → refuse, mark `strandedOnBase`, caller BLOCKs (never "completion theater"); guard's own failure warns-and-continues | `git-workflow-service.ts:528-554` |
| Idempotent-to-partial-git harness | Three-tier probe when the tree is clean: (1) unpushed local commits → format+amend, continue; (2) remote branch ahead of base → coder already pushed, add fixups as *new* commit; (3) neither → nothing to do, return null. All comparisons vs the item's **actual** base, never hardcoded main | `git-workflow-service.ts:556-616`, `:1527-1702` |
| Rebase → push | `fetch <base>` → `rebase origin/<base>`; success ⇒ push `--force-with-lease`; **conflict ⇒ capture conflicting files, `rebase --abort`, push anyway without rebase + report**; push failure degrades gracefully (commit survives) | `git-workflow-service.ts:621-694` |
| Idempotent PR | Persisted-PR short-circuit → `gh pr list --head <branch>` pre-check (also cures created-but-crashed) → **0-commits-ahead skip** → `gh pr create` (array-args, backoff retry) → "already exists" recovery via `gh pr list --state all` | `git-workflow-service.ts:1896-2157` |
| Single-claim | In-memory map, check-and-set with **no await between check and set** (atomic under JS event loop); re-entry exempt | `execution-service.ts:363-390` |
| Dispatch gates | claim-before-dispatch set, WIP cap, hot-file overlap defer, stagger, self-healing "starting" timeout | `feature-scheduler.ts:440-663` |
| Foundation deps (P2) | Dependent won't start until foundation **merged**, not PR-open — kills same-scaffold cascades | `resolver.ts:334-375`, lifecycle doc |
| Worktree lock (P2) | `.automaker-lock {pid,itemId}`; stale (dead PID) = unlocked; removal **pushes unpushed commits first and aborts removal if push fails** | `worktree-lock.ts`, `worktree-lifecycle-service.ts:201-313` |
| Git identity | Injects committer name/email via env + local config — agent containers often have none; commits fail otherwise | `auto-mode-service.ts:3177-3183` |
| Commit message | Deterministic, no LLM: `feat: <title>\n\n…\nItem ID: <id>`; hooks skipped (`--no-verify`) — CI is the verification layer | `git-workflow-service.ts:1310`, `:1371-1381` |

Consciously **not** porting: epic-branch orchestration, prettier passes, changeset/lockfile
automation, auto-merge stage (Quinn + the protoPatch gate own merge on our side), the
smart-LLM branch namer.

## 3. Target architecture

`delegate_to(target, query, item_id=…)` against an acp delegate with `manage_git: true`:

```
framework:  item_id = caller-supplied, else sha1(query)[:12]   # identical fan-out dedups itself
            CLAIM: module-global registry check-and-set (async, no await between) —
                   duplicate in-flight item_id → return the existing run/PR, don't dispatch
            pre-flight: gh pr list --head <branch> → PR already open for this item? return it
            branch = <branch_prefix>/<slug(query,50)>-<last7(item_id)>     # deterministic, NO LLM
            in the delegate's workdir:
              git fetch origin <base>
              git checkout -B <branch> origin/<base>       # never HEAD; reset stale 0-ahead reuse
              ensure scratch excluded: .proto/ etc. → .git/info/exclude; git identity set
coder:      ACP prompt = task + "edit files and run tests only — do NOT branch/commit/push
            or open a PR; the framework owns git."          # instruction, not assumption
framework:  isolation guard: HEAD != <base> or refuse (stranded_on_base — caller must not
            report success)
            secret scan + scratch check on the diff → block commit on findings
            stage (git add -A; .gitignore + info/exclude own exclusions)
            commit on the coder's behalf (deterministic message, --no-verify, injected identity)
            idempotent to partial git: coder committed? amend-path. coder pushed? fixup-path.
            rebase on fresh origin/<base>; conflict → abort rebase, push as-is, report files
            push --force-with-lease (backoff retry incl. .git lock contention)
            VERIFY: ls-remote SHA == local HEAD, else report failure — never "probably pushed"
            gh pr create (idempotent: 0-ahead skip; already-exists reuse)
            release claim; return {pr_url, branch, pushed_sha, rebase_conflicts?}
```

### Design decisions (Phase-0 questions, now decided)

- **Item identity:** optional `item_id` arg on `delegate_to`; default `sha1(query)[:12]`.
  Hash-default means a naive fan-out of the *same task text* to three coders dedups
  automatically — exactly roxy's duplicate-PR failure. Explicit `item_id` for callers with
  stable work-item ids (board issues).
- **Staging:** `git add -A`; exclusion is structural (`.gitignore`, per-worktree
  `.git/info/exclude` seeded by the harness with `.proto/` and friends), matching current
  protoMaker. Plus a harness-side pre-commit secret/scratch scan (research req.).
- **Config surface:** new acp-delegate fields via `FieldSpec` + `Delegate` +
  `AcpAdapter.parse()` (`plugins/delegates/adapters.py`), mirroring `timeout_s`:
  `manage_git: bool = false` (old behavior stays default for non-worktree setups),
  `base_branch: str = "main"`, `branch_prefix: str = ""` (empty → delegate name).
- **Push/PR auth:** reuse `tools/gh_cli.py::run_gh` (injects `GH_TOKEN`/`GITHUB_TOKEN`) and
  the container-env credential rewrite already documented in the guide.
- **Where it lives:** `plugins/coding_agent/git_harness.py` — plugin-local, portable, next to
  the ACP client library; `AcpAdapter.dispatch` calls it around the prompt when
  `manage_git`. Built on `tools/shell.py::run_command` (async, structured errors, cwd/env,
  process-group kill — the repo's subprocess convention).
- **Claim registry concurrency:** module-global dict in `plugins/delegates/`; the check-and-set
  is atomic because `delegate_to`/dispatch are **async** (LangGraph ToolNode runs a turn's
  async tools via `asyncio.gather` on one loop; background delegations are `create_task` on
  the same loop). Invariants: the tool stays `async def`, no `await` between check and set,
  and the claim is taken **inside the dispatch coroutine before the background semaphore** so
  foreground + background fan-out share one registry. If it's ever made sync/threaded, it
  needs a real lock.

## 4. Phased execution

**Phase 0 — ADR (0.5d, mostly done by this due diligence).** Write the MADR ADR for the acp
`manage_git` mode: the decisions above, the protoMaker citations table, the three research
hardening requirements, and what we consciously don't port. Note ADR 0024/0025/0033 lineage
(this extends the acp delegate, it does not revive `code_with`).

**Phase 1 — deterministic branch + setup step.** Branch minting
(`<prefix>/<slug>-<last7(id)>` with git-ref validation) + the pre-coder git setup (fetch
base, `checkout -B` off `origin/<base>`, stale-branch reset, `info/exclude` seeding, identity
injection) in `git_harness.py`; wire into `AcpAdapter.dispatch` behind `manage_git`; the ACP
prompt gains the edit-only directive. Unit tests: branch-name determinism/collision-freeness
(pure), setup against a real `tmp_path` git repo fixture (per `tests/test_shell.py` style —
no fake-git fixture exists yet; establish one).

**Phase 2 — post-run git lifecycle.** Isolation guard (`stranded_on_base` as a distinct
result the caller must surface as failure) → secret/scratch scan → stage → commit-on-behalf →
three-tier partial-git detection → rebase (conflict ⇒ abort+push+report) → push
`--force-with-lease` with backoff (covering `.git` lock contention) → **remote-SHA verify** →
idempotent PR via `run_gh` (0-ahead skip, `pr list --head` pre-check, already-exists
recovery). Tests: real temp repo with a bare "origin"; monkeypatch `run_gh` for the PR layer;
cases for coder-did-nothing / coder-committed / coder-pushed / coder-committed-on-base.

**Phase 3 — single-claim + pre-flight dedup.** Module-global claim registry keyed on
`item_id` (async check-and-set), claim released in a `finally`; duplicate in-flight →
return existing run info instead of dispatching. Pre-flight `gh pr list --head <branch>`
before dispatch (catches restarts *and* the crashed-after-PR case). `delegate_to` gains the
`item_id` arg. Tests: fan-out of N identical `delegate_to` coroutines → exactly one dispatch;
background + foreground mix.

**Phase 4 — wire roxy + docs + remove band-aids.** Switch roxy's acp delegates to
`manage_git: true`; remove roxy's fork-local branch-prefix injection; simplify roxy SOUL git
rules (framework enforces them now); rebuild + redeploy. Update
`docs/guides/coding-agents.md`: rewrite §"In a container" (`git push`/`gh pr create` by the
coder) and §"Parallel builds" (`git checkout -b` by the coder) for the managed mode — and fix
the guide's stale `coding_agent: agents:` / `code_with` YAML to the `delegates:` /
`delegate_to` reality while there.

**Phase 5 — dogfood + verify.** Re-run the outstanding items (399.5b data-loss fix, 394.3
VRAM chart) through the deterministic path — expect one branch/PR per item, no strays, no
collisions, always-pushed (verified SHA). Confirm parallel fan-out of independent items
produces N clean PRs.

**Phase 6 (P2, later).** Hot-file overlap defer; foundation-dependency gating
(merged-not-open); PID worktree lock + safe reaping (push-before-remove, abort-on-push-fail);
merged-PR drift reconciliation.

## 5. Related outstanding work (do via the new path once built)

- **399.5b (#421)** — OPEN, blocked on a **real** finding the calibrated gate caught: `high /
  data-loss: server-side cell-map dedup silently drops multi-harness/judge results` (in
  LabBoard). Needs a genuine fix (key the cell map by harness/judge too, or intentionally
  pick one and document it). Not a waive.
- **394.3 (VRAM chart)** — UNBLOCKED (lab team pushed `vram_gb` to all 53 rows, protoLab#14
  closed, commit `de1ce89`). Re-enable `QualityVramChart` reading `row.vram_gb` (the page has
  a TODO placeholder). 394.2 (HF-feed board) already merged.
- **Compare feature** — 399.1/2/3/4/5a/5c all merged; only 399.5b remains (see above).

## 6. Fleet state / context

- **roxy-protocontent** (ava, `http://ava:7872`, ai_default): coordinator on
  `protolabs/reasoning`. Coder pool `proto-1..3` in worktrees `wt-1..3`
  (entrypoint-provisioned, off origin/main). Delegate to **matt** (a2a via jon hub,
  `JON_API_KEY`). SOUL: coordinator + dedup + DoD rules. Repo local at `/home/josh/dev/roxy`
  — **changes stay local** (fork `origin` is 722/1568 diverged; do not push). The
  branch-prefix band-aid to remove in Phase 4 lives here.
- **matt** (jon fleet member, `protolabs/reasoning`, component-author subagent also
  reasoning): DS engineer; delegate `roxy-protoContent` in his config.
- **jon** image rebuilt with async `delegate_to(background=True)` (#1837). **Quinn**
  (`protoquinn` app, lives in protoWorkstacean — still active though the repo is sunsetting)
  auto-merges on green and now honors unresolved threads (#903).
- **protoContent gate (policy B, restored):** branch protection `enforce_admins`, strict,
  required checks `check` + `review`; conversation-resolution **off**. The `review` check
  runs a **calibrated** protoPatch gate — `.github/scripts/protopatch-gate.py` (merged #420):
  blocks only HIGH/MEDIUM in real correctness/security categories **in the diff**;
  test-gap/maintainability/style/out-of-diff inform, don't gate. `.proto/` is gitignored in
  protoContent now.
- Onboarding: `roxy/scripts/onboard-repo-review-gate.sh <owner/repo>` applies policy-B
  protection to any repo roxy ships to.

## 7. Upstreaming

The deterministic git harness is a **general** protoAgent capability → land it in
`protoLabsAI/protoAgent` (PR), then roxy's local fork picks it up. Only roxy's *config*
(worktree pool size, protoContent scope) stays local. Docs already contributed: coding-agents
"in a container / gateway" (#1839), "parallel builds via a worktree-backed coder pool"
(#1844) — Phase 4 extends both with the managed-git lifecycle and fixes the stale
`code_with` example.

## Appendix: key protoAgent integration points

| Concern | Location |
| --- | --- |
| `delegate_to` tool (gains `item_id`) | `plugins/delegates/__init__.py:33-75` |
| Background delegation path (claim must sit inside dispatch, before the semaphore) | `plugins/delegates/__init__.py:78-125`, `background/manager.py:158-216` |
| `AcpAdapter` config schema / parse / dispatch / teardown | `plugins/delegates/adapters.py:471-674` |
| `Delegate` dataclass (new fields) | `plugins/delegates/adapters.py:58-88` |
| ACP client library (harness lives beside it) | `plugins/coding_agent/__init__.py`, `acp_client.py` |
| Subprocess wrapper to build on | `tools/shell.py:38-98` |
| `gh` runner (token injection) | `tools/gh_cli.py:28-66` |
| Guide sections to rewrite | `docs/guides/coding-agents.md` §container git, §parallel builds |
| Test patterns (real subprocess / fake ACP agent) | `tests/test_shell.py`, `tests/test_coding_agent_plugin.py:70-136` |
