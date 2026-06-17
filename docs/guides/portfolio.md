# Portfolio — one PM across many team boards

Run **one PM (program-manager) agent** that orchestrates work across **many
team-agents**, each running its own [project board](/guides/coding-agents) for its
own repo. The PM dispatches features to a team's board, reads a team's state, sees a
bounded cross-board rollup, and watches for changes — all over A2A, on the
[fleet](/guides/fleet) spine. This is the **scale-out** model: multiplicity lives in
the fleet, not inside one board (see [ADR 0055](../adr/0055-multi-team-orchestration-federated-boards.md)).

The `portfolio` plugin is **pure composition** — it adds no new dispatch or registry
machinery, just tools over three things you already have: the fleet (the team-agent
registry), [delegates](/guides/delegates) (the A2A dispatch primitive), and
`project_board` (the board read).

## Setup

1. **Each team is its own agent.** Stand up a protoAgent instance per repo/team with
   the `project_board` plugin enabled, pinned to that repo (its `.beads`). On its own
   host it's just `plugins: { enabled: [project_board, delegates] }`; for an isolated
   co-located instance, scope it (`PROTOAGENT_INSTANCE`, see
   [multi-instance](/guides/multi-instance)) and set `project_board.db_path`/`repo`.
2. **Enable `portfolio` on the PM:** `plugins: { enabled: [portfolio] }` (it ships
   disabled — enabling is the trust decision). The PM also needs `delegates` for the
   A2A dispatch path.
3. **Register each team-agent as a remote fleet member** — Discover → *Add to this
   fleet* in the console, or `POST /api/fleet/remotes {name, url, token}`. The board is
   addressed by that **name**. The stored bearer authenticates both the team's `/a2a`
   (dispatch) and its `/api/plugins/project_board` (read) — one credential, the right
   direction (PM→team).

> On loopback with no `auth.token` set, a team-agent runs in open mode (the
> default-deny A2A auth is a no-op) — handy for local testing. Across a LAN/tailnet,
> give each team-agent an `A2A_AUTH_TOKEN` and register it with that token.

## Tools

| Tool | What it does |
|---|---|
| `portfolio_boards()` | List the team boards (remote members) — name, url, reachability |
| `portfolio_dispatch(board, title, spec, …)` | Send a feature to a team board over A2A. The team's lead creates + readies it on **its own** board; the team's loop ships the PR in **its** repo |
| `portfolio_board_read(board[, state])` | Structured read of one team board |
| `portfolio_rollup([boards])` | **Bounded** cross-board view — per-board lane counts + only the blocked / critical-path items, never the full feature list. Reason over many boards without raw reads |
| `portfolio_diff([boards])` | What **changed** since the last check — features merged / newly-blocked / unblocked / new — then advances the baseline |
| `portfolio_watch([interval_min, boards])` | Records a baseline, then hands you the `schedule_task` call to run `portfolio_diff` on a schedule |

## Deltas without polling

`portfolio_diff` is a **pull-diff**: the PM keeps a per-board snapshot (feature →
state/blocked) under its instance data root and reports only the transitions. The
first call records a baseline and reports nothing; every call after surfaces just the
changes.

To stay current without burning the PM's reasoning loop on polling, schedule it:

```
portfolio_watch(interval_min=15)
# → records a baseline + returns:
#   schedule_task(prompt="Run portfolio_diff and report any board changes; if none, do nothing.",
#                 when="*/15 * * * *")
```

Each scheduled fire arrives as a turn carrying only the deltas — *the system polls for
the PM*. (Why pull-diff and not push: A2A push notifications are task-scoped, the event
bus is in-process, and a team-agent doesn't know its PM — so a PM-side snapshot+diff is
the thin correct shape. See [ADR 0055 P2](../adr/0055-multi-team-orchestration-federated-boards.md#phasing).)

## Notes

- A board id **is** the team-agent's fleet name — no separate registry.
- An unreachable board never sinks a rollup/diff: it's reported with an `error` and the
  others still return.
- Board isolation (ADR 0055 P0) means each team's board writes to **its own** repo's
  `.beads` — the PM only ever touches a team's board over A2A.
