# 0067 — Standalone `watch` primitive (many concurrent watches)

Status: **Accepted**

> Resolves the fork-in-the-road ADR 0030 left open ("a separate watch primitive … open to
> the inverse call in review"). A supervisor agent needs to babysit **many** external
> processes **at once**; the monitor-goal *disposition* can't, because a goal is keyed
> one-per-session. This adds a first-class `watch` primitive and ties it to
> `sdk.run_in_session` (ADR/#1494) so a met watch can run a follow-up agent turn.

## Context

ADR 0030 gave goals a `monitor` disposition: an out-of-band, verifier-only objective the
agent *supervises* rather than *drives*. It was the right shape for **one** standing
watch, but it lives inside `GoalState` and `GoalStore`, which key **one goal per session**.
That imposes two limits that a supervisor agent hits immediately:

1. **One watch per session.** You can't watch a deploy *and* CI *and* a treasury *and* a
   backlog concurrently from one context.
2. **Session-coupling.** ADR 0030 D2 itself notes monitor evaluation is *"decoupled from
   sessions and turns"* — it polls global state — so keying it by `session_id` is an
   impedance mismatch.

`GoalState` also became a union type: half its fields are drive-only
(`iteration`/`max_iterations`/`no_progress_streak`/`checklist`/`abandon_reason`), half
monitor-only (`deadline`/`stall_after`/`stall_streak`/`last_checked`) — dead weight either
way.

## Decision

Add a standalone `watch` primitive. A **goal** stays what it is best at — the agent
*drives* a bounded loop toward it (`drive` mode). A **watch** is the passive counterpart:
*poll a condition on a cadence; when it trips, react* — and you can hold as many as you
like.

### D1 — `Watch` + `WatchStore` (keyed by watch id, many per instance)

```
Watch = { id, condition, verifier, interval_s, status,
          deadline?, stall_after?,                       # termination / stall (as ADR 0030 D5)
          run_prompt?, run_session?,                     # reaction: enqueue a turn (D3)
          created_at, last_checked, last_reason, last_evidence, stall_streak, ... }
```

`WatchStore` writes one JSON per **watch id** (not session), `PROTOAGENT_INSTANCE`-scoped
exactly like `GoalStore`. So an agent/instance holds N concurrent watches; `list()` returns
them all. Verifiers are **reused verbatim** from `graph/goals/verifiers.py` (plugin /
command / test / ci / data / llm) — no new verification surface.

### D2 — Out-of-band tick

A `_watch_loop` (mirroring `_monitor_goals_loop`) evaluates every active watch on a cadence
(`watch_interval` default 30s; a watch may set a shorter per-watch `interval_s`), verifier
only, no agent turn. Met → finish + react; deadline passed → `expired` (fires `on_expired`);
`stall_after` consecutive unchanged checks → `on_stalled` once per episode (watch stays
active).

### D3 — Reaction = `run_in_session` + hooks (the supervision payoff)

When a watch is **met**, two reactions fire, either/both:

- **Run a follow-up turn.** If the watch carries `run_prompt`, the controller enqueues it as
  a one-shot agent turn via `sdk.run_in_session(run_session, run_prompt)` (ADR/#1494) — the
  agent *acts* on the trip ("deploy finished → run the smoke test"), non-blocking.
- **Fire hooks.** `register_watch_hook(on_met=…, on_stalled=…, on_expired=…)` lets a plugin
  react in-process (notify, record, set the next watch).

This is the parallel-supervision engine: N watches, each with its own trip-action.

### D4 — Set-paths mirror the goal trust model

- **Agent tool** `create_watch(condition, check, …)` / `list_watches` / `clear_watch` —
  `plugin`-verifier only (same posture as `set_goal`, ADR 0028 D3): the agent can't open a
  shell/eval watch.
- **Operator** `POST/GET/DELETE /api/watches` — accepts **any** verifier type, safe because
  `/api` is operator-tier by the ADR 0066 path ceiling (the operator channel).
- **SDK** `sdk.create_watch(...)` + `register_watch_hook(...)` for plugins.

### D5 — Relationship to the monitor-goal disposition

Watches **supersede** ADR 0030's `monitor` mode — and the migration is **now done**: goal
`monitor` mode, the `_monitor_goals_loop` tick, `GoalState`'s union fields
(`mode`/`deadline`/`stall_after`/`last_checked`/…), the `expired` status, goal `on_stalled`,
and `sdk.start_goal_loop`/`stop_goal_loop` are all **removed**; goals are drive-only. Watching
a metric is a watch (`sdk.create_watch` / `POST /api/watches` / the `create_watch` tool). This
is a **breaking change** for forks that used `start_goal_loop` or a `monitor` goal — they move
to `create_watch` + `register_watch_hook`.

## Consequences

- A supervisor agent holds **many** concurrent, independently-reacting watches.
- `watch` + `run_in_session` compose into event→action supervision without bespoke glue.
- `GoalState` is now **drive-only** — the monitor union fields are gone.
- The goal drive loop, `set_goal`, and `run_in_session` are untouched; only the goal
  `monitor` disposition (and its `start_goal_loop` helper) were removed.

## Alternatives considered

- **Keep the disposition, lift the one-per-session limit inside `GoalStore`.** Rejected:
  it drags the whole drive-loop union type along and keeps session-coupling; the concept is
  cleaner as its own primitive.
- **Reuse the scheduler directly (a cron job whose prompt is "check X").** That's the ADR
  0028 anti-pattern ("storms the loop") and has no verifier/terminal/stall model — a watch
  is verifier-first with typed termination.

## Slices

- **PR1 (this ADR)** — the engine: `Watch` + `WatchStore` + `WatchController`
  (create/list/clear/evaluate/tick) + hooks + the `_watch_loop` tick + agent tools + tests.
- **PR2** — operator `/api/watches` + `sdk.create_watch`.
- **PR3** — console **Watches** panel (list/status/clear; DRAFT, local-test gate).
- **Migration (done)** — removed goal `monitor` mode, `start_goal_loop`/`stop_goal_loop`, the
  monitor tick, and `GoalState`'s union fields; goals are drive-only. `create_watch` is the
  replacement.
