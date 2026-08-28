# 0109 — project_board: one board, one store, N repos

- Status: Accepted (umbrella #3237, decision D4)
- Date: 2026-08-28
- Supersedes: **in part, in place** — the one-repo binding inside
  [ADR 0055](./0055-multi-team-orchestration-federated-boards.md)'s single-board
  sub-decision ("run *this* team's board" read as "this team's *repo*"). The
  single-board shape and the federation decision stand.
- Builds on: [ADR 0042](./0042-fleet-supervisor-unified-console.md) (fleet —
  remote members, `delegate_to` over A2A), ADR 0055 (scale-out federation, P0
  board pinning), [ADR 0065](./0065-two-tier-instance-paths.md) (the instance
  store), [ADR 0095](./0095-managed-projects-registry.md) (managed projects —
  repos as named registry entries).

## Context

ADR 0055 decided *cross-team* multiplicity: each team is its own protoAgent
instance running a single `project_board`; a PM federates over the team-agents
via A2A. Woven through that decision was an equality the plugin inherited from
its origins — **1 board = 1 repo**: the board is pinned to `project_board.repo`,
the bead carries no repo, and ADR 0095 still records the config as "one repo per
instance."

Real teams own more than one repo — a service plus its plugin, console, or infra
repos. Under 1 board = 1 repo, that team needs N instances and N boards, which
pushes *intra-team* coordination up into the PM/portfolio tier. That is the
wrong tier: the fleet exists to federate across **teams** (ADRs 0042/0055), not
to stitch one team's second repo back onto its first.

The umbrella design (#3237) already decided the new boundary (D4) and what
`db_path` names (D3). This ADR records both; it does **not** reopen the
federation decision.

## Decision

**One board, one store, N repos.** A `project_board` instance keeps exactly one
board backed by exactly one beads store — but the board is no longer bound to a
single repo. A feature carries its **target repo**, and the plugin routes each
dispatch (worktree, coder, PR, merge reconciliation) to that feature's repo.

- **Cross-repo routing is the plugin's job.** "Which repo does this feature
  build in" is board-local state, resolved per feature inside `project_board`.
  `project_board.repo` remains the default for features that don't name one.
- **Cross-team federation remains the fleet's job.** Nothing moves into the
  plugin from the portfolio tier: a PM still addresses team-agents as remote
  boards over A2A (ADRs 0042/0055), and rollups/deltas/dispatch are unchanged —
  a board a PM addresses may now simply span repos.
- **Pinning follows the D3 `db_path` meaning.** `db_path` names a location in
  the **instance store** (the ADR 0065 instance data root), never a repo
  checkout. That is what keeps ADR 0055 P0's pinning deterministic once repos
  are plural: the store belongs to the *instance*, no repo hosts board state,
  and a checkout carries no board identity. The one-repo *reading* of P0 ("the
  board's beads location is its repo's") is gone; the P0 invariant — explicit,
  instance-pinned, never cwd auto-discovery — survives unchanged.

## Non-goal

**No in-process board registry.** One instance still runs one board; we do not
build a `boards:` list or N stores in one process. ADR 0055's scale-up
rejection stands: multiplicity across teams is the fleet, over A2A.

## Consequences

- ADR 0055's single-board sub-decision is superseded **in part, in place**:
  "single board, no registry" stands; "for its repo" does not.
- Repo identity lives on the work item (the bead), not on the singleton —
  landing ADR 0055's "repo identity must live … ideally on the bead" where it
  belongs, so rollups and merge reconciliation stay unambiguous per feature.
- A team with N repos is one instance, one board, one loop. The portfolio tier
  is unaffected; nothing above the board needs to know how many repos it spans.
- The beads store lives in the instance store; repos never host `.beads/`.
