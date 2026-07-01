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
2. **Install + enable `portfolio` on the PM.** It's a standalone plugin
   ([`protoLabsAI/portfolio-plugin`](https://github.com/protoLabsAI/portfolio-plugin)),
   shipped in the **pm-stack** bundle alongside `project_board` — or install it directly
   (`POST /api/plugins/install`). Then `plugins: { enabled: [delegates, portfolio] }` (it
   ships disabled — enabling is the trust decision; `delegates` is the A2A dispatch path).
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
| `portfolio_link(from_board, from_feature, to_board, to_feature[, note, title, spec, …, remove])` | Record (or remove) a cross-board dependency; with `title`+`spec` it's a *planned dispatch* (held work) |
| `portfolio_plan()` | The cross-board dependency graph + what's ready to dispatch next |
| `portfolio_autodispatch([dry_run])` | Create each planned link's held work once its blocker ships — idempotent, schedulable |

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

## Cross-board dependencies

When one team's feature can't start until another team's ships, record the edge and
let the PM sequence:

```
portfolio_link(from_board="team-web", from_feature="bd-aaa",
               to_board="team-api",  to_feature="bd-bbb",
               note="web's probe wiring needs the /v2 endpoint")
# team-web:bd-aaa is now blocked until team-api:bd-bbb is done (merged)

portfolio_plan()
# → every link tagged satisfied / blocking / unknown / dangling, plus:
#   ready_to_dispatch — from-features whose every blocker has merged (dispatch these next)
#   blocked           — the rest, with their open blockers
```

Edges are **PM-side** (`portfolio_links.json`, scoped to the PM) — features are
addressed by `(board, feature_id)` since ids are board-local. A link is **satisfied**
the moment its depended-on feature reaches `done` (the board's only merge signal); an
unreachable blocker is **unknown** and fail-closed (never dispatched on a guess); a
vanished one is **dangling** (prune with `portfolio_link(remove="lnk-…")`). Cycles are
rejected when you add the link.

### Holding work until its dependency ships (auto-dispatch)

A plain link is advisory — but a team's spawn loop would still start a "ready" dependent
before its cross-repo blocker lands. To actually **gate** the work, give the link the
dependent's spec (a *planned dispatch*): the work isn't created on its board until the
blocker ships, then `portfolio_autodispatch` creates it.

```
portfolio_link(from_board="team-web", from_feature="render-v2",   # a planning label
               to_board="team-api",  to_feature="bd-bbb",
               title="Render users from /v2",
               spec="Wire the web users page to /v2/users")
# nothing is created on team-web yet — the work is held behind bd-bbb

# team-api ships bd-bbb (→ done); then, manually or on a schedule:
portfolio_autodispatch()        # dry_run=True to preview
# → creates "Render users from /v2" on team-web NOW, marks the link dispatched
```

`portfolio_autodispatch` is **idempotent** (a `dispatched` flag stops it re-creating), so
schedule it with `schedule_task` and the PM dispatches each held feature the moment its
dependency lands — no polling, no jumping the gun. See
[ADR 0055 P3](../adr/0055-multi-team-orchestration-federated-boards.md#phasing).

## Spinning up teams on demand

The Setup above assumes standing team-agents you register as remotes. A PM can also spin
teams up **ephemerally** — one per project, disposed when its board drains:

```python
portfolio_spinup_team(name="docs-team", repo="/abs/path/to/repo")  # clone a team config into a
                                                                   # scoped workspace, boot it,
                                                                   # register it as a board
portfolio_dispatch(board="docs-team", title=…, spec=…)             # send it work over A2A
portfolio_autodispose()                                            # dispose teams whose board is drained
# also: portfolio_teardown_team(name) to dispose one now, portfolio_teams() to list them
```

**Gateway inheritance (v0.14+): no creds prep.** A spawned team inherits the PM's own
resolved model gateway — the PM's `model.api_base` (resolved through the ADR 0047
App→Host→Agent cascade, so a box-level gateway counts) plus its `OPENAI_API_KEY`, which
reaches the team via its environment. So `portfolio_spinup_team` runs real turns out of the
box with **no `team_template`**. Pass `template=`/`portfolio.team_template` only for a team on
a *different* gateway than the PM's; spinup preflights the assembled config and fails loudly
(rolling the workspace back) if a team couldn't reach a model, rather than booting a mute one.
The host needs the `br` (beads) CLI for the team's board.

## Notes

- A board id **is** the team-agent's fleet name — no separate registry.
- An unreachable board never sinks a rollup/diff: it's reported with an `error` and the
  others still return.
- Board isolation (ADR 0055 P0) means each team's board writes to **its own** repo's
  `.beads` — the PM only ever touches a team's board over A2A.
