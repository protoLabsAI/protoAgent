# 0030 — Monitor goals: cadence-evaluated, hook-reactive objectives (+ per-goal no-progress limit)

Status: **Accepted** (sliced — see Slices)

> **Pulled upstream from the protoTrader-in-space fork**, where an autonomous agent
> grows a treasury via a background engine. Authored there as ADR 0029; renumbered to
> **0030** on upstream (0029 is the [communication-plugin standard](0029-communication-plugins-standard.md)).
> Implements the slice [ADR-0028](0028-plugin-goal-verifiers.md) deferred as **D6**
> ("Out-of-band evaluation") — so forks running long-horizon, externally-driven
> objectives don't each re-invent the workaround.

## Context

ADR-0028 gave plugins a clean way to ground-truth domain state (`register_goal_verifier`,
the `plugin` verifier type, lifecycle hooks). It explicitly **deferred** the deeper gap as
D6:

> Goals evaluate only after a terminal turn *in their session*. Progress made out-of-band
> (a background engine, a scheduler tick in another context) never triggers evaluation, so
> a met goal can sit `active` indefinitely.

and warned, precisely:

> the goal continuation loop is built to drive a session to done in a bounded number of
> iterations, **not** to poll a distant target — using it for the latter storms the loop.

We hit exactly that, live. A `{"type":"plugin","check":"spacetraders:credits","args":{"min":1000000}}`
goal went **`exhausted` in minutes**: goal-mode re-invoked the agent up to `max_iterations`
(default **8**) to "keep working toward the goal," but the **background engine** — not the
agent's reasoning — earns the credits, over *hours*. The agent's turns can't move the
number, so the loop burned its budget and quit while the real work was nowhere near done.

This is **not game-specific.** The same shape recurs whenever the agent's job is to **start
and supervise** an external process, while a metric crosses a threshold **over time, driven
by something other than the agent's turns**:

- a training run reaching a target loss
- a deployment finishing a rollout
- a pipeline draining a backlog
- a market-maker / trading bot hitting a P&L target

For all of these today, goal-mode is the wrong tool and there is no right one: the
iteration budget exhausts, no-progress false-fires on a flat-but-rising metric, and there's
no "just watch and react" disposition. ADR-0028's verifier + `on_achieved` hook are the
right *parts*; what's missing is a goal **disposition** that uses them **passively**.

## Decision

Add a second goal **disposition** alongside today's agent-driven one, plus the cadence
evaluation D6 sketched. Small and additive: existing goals are unchanged (the default
disposition is the current behavior).

### D1 — a `monitor` disposition on the goal spec

A goal carries `mode: "drive" | "monitor"` (default `"drive"` = today's bounded
agent-iteration loop). A `monitor` goal asserts: *an external process drives the metric;
the agent is not the one doing the work.*

```jsonc
/goal {
  "condition": "grow the treasury to 1,000,000 credits",
  "mode": "monitor",
  "verifier": {"type": "plugin", "check": "spacetraders:credits", "args": {"min": 1000000}}
}
```

A `monitor` goal:

- is **not** added to the agent continuation loop — setting it does **not** re-invoke the
  agent, and the controller never emits a `continue` Decision with "keep working toward the
  goal." (The agent has nothing to do; the engine does.)
- does **not** consume / exhaust an iteration budget. It runs until **achieved**, **cleared**,
  or an optional **`deadline`**.
- reacts purely via the **hooks** from ADR-0028 D4: `on_achieved` fires when the verifier
  passes. (Optionally `on_stalled` — see D5.)

### D2 — cadence (out-of-band) evaluation

`monitor` goals are evaluated **without an agent turn** — the gap D6 named. Two mechanisms,
both verifier-only (no model call):

1. **A built-in scheduler tick.** The substrate schedules a periodic `evaluate-monitor-goals`
   job (interval configurable, e.g. `goal_monitor_interval: 60s`) that runs each active
   monitor goal's verifier and `_finish`es the ones that pass. Works out of the box.
2. **`controller.evaluate_now(session_id)`** — an event-driven hook a plugin calls when its
   own state changes (e.g. right after a sale clears), so achievement is detected promptly
   instead of waiting for the next tick. Optional optimization on top of (1).

Crucially, monitor-goal evaluation is **decoupled from sessions and turns**: the verifier
checks live/global state (the live API, a file, a metric), so the tick iterates active
monitor goals across all sessions and runs their verifiers directly. This is what makes
"a met goal can sit `active` indefinitely" go away.

### D3 — no exhaustion semantics for monitor goals

`drive` goals keep `iteration >= max_iterations → exhausted` and `no_progress_streak >= limit
→ unachievable` (the loop must terminate). `monitor` goals skip both: a long-horizon target
is *expected* to sit unmet across many checks while the external process works, and a
flat-but-rising metric must not read as "no progress." A monitor goal ends only on
**achieved**, **cleared**, or an explicit **`deadline`** (→ `expired`, firing `on_failed`).

### D4 — per-goal `no_progress_limit` (stands alone)

Independent of monitor mode: `max_iterations` is already per-goal (`GoalState.max_iterations`),
but `no_progress_limit` is **config-only** (`getattr(self._config, "goal_no_progress_limit", 3)`).
Make it per-goal too (`GoalState.no_progress_limit`, defaulting to the config), so a single
`drive` goal can widen its own patience without changing the global default. Tiny, additive,
useful regardless of D1–D3.

### D5 — stall signal (optional)

A monitor goal may set an optional `stall_after` (N checks with unchanged evidence) that
fires an **`on_stalled`** hook — *without* ending the goal. This surfaces "the background
engine stopped earning" as an actionable signal (notify, record a finding, set a remediation
goal) while keeping the objective alive. Strictly optional; omit in the first slice.

## Consequences

- Long-horizon, externally-driven objectives become **first-class and safe**: set once, the
  agent supervises, the metric is polled out-of-band, the `on_achieved` hook reacts. No
  "storming the loop," no premature `exhausted`.
- The autonomous loop stops depending on driving a turn in the goal's session to notice
  completion — closing ADR-0028 D6.
- Additive surface: one spec field (`mode`), one scheduler tick, one optional
  `evaluate_now`, one per-goal field (`no_progress_limit`). `drive` goals are untouched.
- Clear division of labor: `drive` = the agent *is* the work (bounded loop); `monitor` =
  the agent *supervises* the work (unbounded watch). Operators pick by disposition, not by
  fighting the iteration budget.

## Alternatives considered

- **Just raise `max_iterations` / `no_progress_limit` (the workaround we used).** Keeps the
  goal `active`, but the agent is still told to "keep working" every iteration (wasted turns —
  it has nothing to do), evaluation still rides on session turns, and a huge budget on a
  rapid loop just delays the storm 0028 warned about. A band-aid, not the model.
- **A separate "watch" primitive, leaving goal-mode purely agent-driven.** Viable, and a
  real fork in the road for the team: a standalone scheduled verifier+hook with no goal
  object. Rejected *as the lead* because it duplicates the goal store, status surface, and
  hook plumbing for what is conceptually still "an objective with a testable outcome" — a
  disposition reuses all of it. (Open to the inverse call in review.)
- **Keep it operator-only / drive turns manually.** That's the status quo escape hatch ("drive
  a turn in the goal's session") — it works for a human at the helm but defeats the point of
  an *autonomous* agent owning a standing objective.
- **An external monitor service (cron + MCP).** Out-of-process and heavier for something the
  controller + a verifier already do in-process; the scheduler tick is simpler and reuses
  ADR-0028's hooks.

## Slices (vertical, smallest-useful-first)

- **PR1** — per-goal `no_progress_limit` (D4). Tiny, independently useful, unblocks wider
  patience without the rest.
- **PR2** — the `monitor` disposition + the scheduler evaluate tick (D1, D2.1, D3). The core:
  makes long-horizon objectives work end-to-end via ADR-0028 hooks.
- **PR3** — `controller.evaluate_now(session_id)` for prompt event-driven detection (D2.2).
- **Future** — `on_stalled` / `deadline → expired` (D5).

## Reference implementation

Sketch (the team owns the real shape):

- `GoalState`: add `mode: str = "drive"`, `no_progress_limit: int | None = None`,
  optional `deadline`, `stall_after`.
- `controller.evaluate`: if `state.mode == "monitor"`, run the verifier; on met →
  `_finish("achieved")` (hooks fire as today); else record `last_evidence` + `last_checked`
  and return **no** `continue` Decision (no agent re-invocation, no iteration/no-progress
  bookkeeping). `drive` path unchanged.
- A scheduler job (registered at server init when any monitor goal is active, or always-on
  at `goal_monitor_interval`) that calls `controller.evaluate` for each active monitor goal —
  verifier-only, no turn.
- `controller.evaluate_now(session_id)` → run the active goal's verifier immediately; a
  plugin calls it from its own state-change path.
- `parse_control` / `set_goal_safe`: accept `mode` (and pass `no_progress_limit` through).
  `monitor` stays compatible with the ADR-0028 D3 safe-programmatic-set gate (it's still a
  `plugin`-verifier goal — no new code-exec surface).
