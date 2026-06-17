# 0055 — Multi-team orchestration: federated boards over A2A (scale-out)

- Status: Accepted
- Date: 2026-06-17
- Builds on: ADR 0042 (fleet — slug-routed agents, remote members, `delegate_to`
  over A2A, mDNS/tailnet discovery), ADR 0024/0025 (CLI coding agents over ACP +
  the unified delegate registry), ADR 0039 (plugin event bus), ADR 0043 (plugin
  consumption SDK), and the `project_board` plugin (beads-backed coding
  orchestration).

## Context

We want **one Project-Manager agent to orchestrate work across multiple project
teams** — many boards, each backed by its own repo, dispatching coding agents
scoped to that repo, so a PM can run a *portfolio* of delivery teams rather than a
single board bound to one repo.

Today the model is **1 board = 1 repo = 1 coder pool**:

- `delegates` (ACP spawn) is **already multi-repo-capable**: the registry holds
  many delegates with distinct `workdir`s; `Delegate.workdir` is overridable
  per-call; the ACP client cache keys on `workdir` (so one coder fans out to N
  concurrent worktrees); `worktree.py` is fully repo-parameterized.
- `project_board` is a **hard singleton**: a process-wide `_BOARD` (store.py:497,
  `get_store()` ignores its kwargs after first init), one `config_section` →
  one repo/coder/beads-db, one `BoardLoop`, board tools take **no board
  argument**, and the bead carries **no repo field** (repo lives only on the
  singleton). It also auto-discovers `.beads/` from the **process cwd**, so a
  board pollutes whatever repo it happens to run in (the dev-env isolation gap,
  found 2026-06-17).

Two further constraints shaped the decision: (1) a single agent **cannot hold N
boards × M features in context** — the data must be streamed/projected, not
dumped; (2) governance ("which agent may touch which repo / secrets / budget") is
a first-class requirement, not an afterthought.

## Decision

**Scale out, not up.** Multiplicity is the **fleet**, not an in-process board
registry:

- **Each repo/team is its own protoAgent instance** running a single
  `project_board` for **its** repo (one board per instance — the singleton stays;
  no in-process board registry).
- **The PM is a portfolio orchestrator** that federates over the team-agents
  **via A2A**, reusing ADR 0042's remote-member + `delegate_to` spine. A "board"
  the PM addresses resolves to a remote team-agent, not a local store.
- **`project_board` stays single-board.** We do **not** build a `boards:` list or
  a board registry inside the plugin. The plugin's job shrinks to "run *this*
  team's board well"; cross-team is the fleet's job.

### Sub-decisions

1. **Deterministic board pinning (P0).** A board's beads location must be
   explicit and instance-pinned — resolved from config / `operator.project_dir` /
   the instance data root, **never** cwd auto-discovery. This makes each
   team-agent's board deterministically *its* repo's, and fixes the isolation
   gap. (The `db_path` config must actually wire through, and the `_BOARD`
   singleton must re-read it on reload.)

2. **Portfolio addressing.** The PM treats each team-agent as a **remote board**.
   Reuse fleet discovery + the remote-member registry (ADR 0042); a board id maps
   to `{agent, repo}`. Dispatching a feature = a `delegate_to(team-agent, …)`
   A2A call; the team-agent runs its own board loop and ships the PR. The PM does
   not reach into any repo directly.

3. **Context at scale = projection + deltas, never raw reads.** The PM holds a
   **portfolio rollup** (per board: `{ready, in_progress, in_review, blocked}` +
   blocked/critical-path items only). It drills into a single board's features
   **on demand** over A2A. Team-agents **push deltas** (PR merged, feature
   blocked, coder failed) into the PM's activity feed via the event bus / A2A
   (ADR 0039) — streamed, bounded context. The "board = projection over beads"
   design makes the rollup cheap.

4. **Governance is per-instance + natural.** A team-agent only has *its* repo,
   secrets, and budget — isolation by construction. The PM's authority is "which
   team-agents it may dispatch to" (its delegate/remote-member set). No new
   cross-repo trust surface in the PM process.

5. **Plugin-SDK / ecosystem.** `project_board` composes `delegates` via a **direct
   import** today (`loop.py:_resolve_delegate` reaches into `plugins.delegates.*`).
   Move this to the ADR 0043 consumption SDK seam (named lookup via `graph.sdk`),
   so plugins compose through a contract, not internals. The portfolio layer is a
   composition of **fleet × project_board × delegates** — the first real
   multi-plugin orchestration, and the template for "how plugins tie together."

## Consequences

- **Leverages the existing fleet/A2A substrate** — remote members, slug routing,
  `delegate_to`, discovery, turn-finished toasts — instead of new machinery.
- **`project_board` is simplified, not expanded** — the singleton is *correct* for
  one-board-per-instance; we delete the cwd-coupling, not add a registry.
- **Deployment model**: a team-agent runs where its repo lives (or with that repo
  checked out + pinned); the PM runs anywhere and federates. Matches "multiple
  project teams" literally.
- **Repo identity** must live on the board/instance record (and ideally the bead)
  so merge reconciliation and rollups are unambiguous across team-agents.
- New work concentrates in: (P0) board pinning, (P1) the PM portfolio capability
  (address team-agents as boards over A2A), (P2) rollup + delta streaming,
  (P3) a cross-repo dependency graph (the Zenflow-class capability).

## Alternatives considered

- **Scale-up: an in-process board registry** (`boards:` list, N `BeadsBoard`s in
  one PM process). Rejected: one process owns every repo's worktrees + beads
  (blast radius, single box), it doesn't model "teams," and it ignores the fleet
  spine we already built. Kept as a possible *co-located* optimization later, not
  the primary.
- **Hybrid (uniform local+remote board addressing).** Deferred — start pure
  scale-out; the addressing abstraction can later admit local boards if a single
  agent ever needs to hold co-located ones.

## Phasing

- **P0** ✅ *(shipped)* — deterministic per-instance board pinning in `project_board`
  (explicit beads location; `db_path` wires through; per-(db,repo) store; `br` runs in
  the configured repo). *Also fixed the dev-env isolation gap.* projectBoard-plugin
  v0.19.0 (#46) + pm-stack pin (#6) + the host fix protoAgent #1105 (scoped instances
  now resolve installed-plugin config from the unscoped plugins dir).
- **P1** ✅ *(shipped + live-verified)* — PM portfolio capability: the `portfolio`
  plugin (#1107) — `portfolio_boards` / `portfolio_dispatch` (A2A) / `portfolio_board_read`.
  Pure composition of fleet × delegates × project_board. Live-verified end-to-end
  against a real team-agent over A2A (a dispatched feature was created on the team's
  own isolated board and read back structured).
- **P2** — rollup + delta streaming (the bounded-context layer).
  - *Slice 1* ✅ *(shipped)* — `portfolio_rollup` (#1110): bounded cross-board view —
    per-board lane counts + only the blocked/critical-path items, never raw boards.
  - *Slice 2* ✅ *(shipped)* — board **deltas** via `portfolio_diff` + `portfolio_watch`:
    a PM-side **pull-diff**. The PM snapshots each board (feature→state/blocked) under
    its instance data root and reports only the transitions (merged / newly-blocked /
    unblocked / new); `portfolio_watch` seeds a baseline and hands back a `schedule_task`
    cron so the diff runs on a schedule and arrives as a turn — *the system polls for the
    PM, not the PM's reasoning loop*.

    **Why pull-diff, not push** (decided after a substrate scout): A2A push notifications
    (`pushNotificationConfig`) are **task-scoped** — they express "my dispatched task
    finished," not "this board changed," which is the wrong granularity. The event bus
    (ADR 0039) is **in-process per instance** with no cross-instance bridge. And remote
    registration is **one-directional** (PM→team via `remotes.json`) — a team-agent has
    no back-reference to its PM, so a push would need new credential distribution + a
    team-side emit hook in the standalone `project_board` repo. Pull-diff reuses the
    slice-1 read path verbatim, auth already flows PM→team, and nothing changes on the
    team side. *Deferred push upgrade (P2.5+):* a team-side bus→`/api/inbox` bridge once a
    team↔PM handshake exists — the inbox (`POST /api/inbox`, `priority:"now"` fires a turn)
    is the right inbound sink, but it's a larger lift for marginal latency gain.
- **P3** — cross-repo dependency graph + program-level sequencing.
