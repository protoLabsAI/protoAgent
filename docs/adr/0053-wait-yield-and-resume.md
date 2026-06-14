# 0053 — `wait`: yield-and-resume instead of busy-polling

- Status: Accepted
- Date: 2026-06-14
- Builds on: ADR 0003 (reactive agent — event bus + durable Activity thread +
  scheduler), ADR 0004 (multi-instance scoping / scheduler owner-lock), and the
  scheduler's one-shot ISO jobs (`scheduler/local.py`).

## Context

An agent doing real-world work constantly waits on things that aren't done yet —
a SpaceTraders ship "arriving in 37s", a build, a deploy, a cooldown. With only a
status tool (`st_ship`, `autopilot_status`, …) the model's instinct is to **poll
it in a loop** until the thing is ready. That loop runs entirely inside one turn
and burns the whole recursion budget: a real session hit
`GRAPH_RECURSION_LIMIT (200)` purely on repeated `st_ship` calls, and every one
of those calls is wasted tokens and latency.

Raising the recursion limit is the wrong fix — we *want* long-horizon tasks, and
a bigger budget just means more spam before the same wall. The agent shouldn't
sit in a turn spinning; it should **yield** and be brought back when the thing is
actually ready.

The machinery for "bring the agent back later" already exists: the scheduler
(ADR 0003) fires a one-shot job by POSTing a `SendMessage` to the agent's own
`/a2a` endpoint in the durable Activity thread, which runs as a fresh autonomous
turn. There was even already a `schedule_task(prompt, when)` tool. Two things
were missing: (a) a tool ergonomically shaped for "wait N seconds then continue",
and (b) **a way for a tool to end the current turn** — `create_agent` has no
built-in "a tool ended the turn" signal, so even a scheduled resume wouldn't stop
the in-turn polling.

## Decision

Add a core **`wait(seconds, then)`** tool plus a **`WaitYieldMiddleware`** that
ends the turn once `wait` has run.

- **`wait` tool** (`tools/lg_tools.py`, in the scheduler-tools builder — it needs
  the scheduler): schedules a one-shot resume at `now + seconds` (ISO-8601
  `add_job`) whose prompt is the agent's self-authored `then` instruction.
  Returns a confirmation. On a scheduling failure it returns an `Error:` string
  (and does **not** yield — see below).

- **`WaitYieldMiddleware`** (`graph/middleware/wait_yield.py`): a
  `before_model` hook with `@hook_config(can_jump_to=["end"])`. When the trailing
  tool-result block of the thread contains a *successful* `wait` ToolMessage —
  i.e. `wait` just ran this cycle — it returns `{"jump_to": "end"}`, so the graph
  ends instead of looping back to the model. It is a strict no-op on every turn
  that didn't call `wait` (on a fresh turn the trailing message is the new
  stimulus, not a `wait` result), and it does **not** yield when `wait` errored
  (status `error` or an `Error:`-prefixed result), so a failed schedule surfaces
  to the agent rather than silently dropping the task.

A one-line operator guideline in the system prompt steers the model to reach for
`wait` instead of polling.

### Why a turn-ending middleware, not "trust the model to stop"

We could just tell the model "after scheduling, stop calling tools". But the
failure mode is *exactly* a model that won't stop (that's how we got here). The
middleware makes the yield deterministic: once `wait` runs, the turn ends —
independent of what the model would do next.

### Where the resume lands

The scheduler fires resumes in the **Activity thread** (`system:activity`), the
established home for autonomous/long-horizon work — durable and visible in the
Activity feed. The agent's `then` instruction plus the Activity history carry the
context forward; a long task becomes a chain of short turns, each ended by `wait`.

Resuming in the *originating* chat session instead (carrying the contextId
through the job) is a deliberate follow-up, not part of this ADR.

## Consequences

- Long-horizon "do X, wait, do Y" tasks no longer burn a turn's recursion budget
  polling; the budget covers actual work.
- `wait` is lead-agent-only (gated on the scheduler; subagents run bounded by
  `max_turns` and don't get it).
- The yield is durable across restart (the resume is a persisted scheduler job).
- Follow-up: optional same-session resume (thread the originating contextId into
  the job so the continuation lands in the chat the user is watching, not only
  the Activity feed).
