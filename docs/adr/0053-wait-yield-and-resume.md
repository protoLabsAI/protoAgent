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

**Same-session resume (added):** `wait` stamps the originating chat session onto
the job (`Job.context_id`), read from the same `tracing.current_session_id()`
contextvar the background-subagent path uses — no extra plumbing. The scheduler
fires into `job.context_id or ACTIVITY_CONTEXT`, so a yield *from a chat* resumes
in **that chat's thread** (the agent wakes with the conversation history intact),
while a plain scheduled/Activity-origin job still lands in the durable Activity
thread. `context_id` is a new, lazily-migrated `jobs` column (existing DBs keep
working); the workstacean (remote) backend accepts the arg for parity but can't
target a context, so it stays Activity-routed there.

This is **agent-side continuity** (Problem A): the resumed turn runs in the chat's
checkpointer thread. **UI surfacing** (Problem B) — making that server-fired turn
*appear live* in the browser chat tab — is separate: the browser only renders
turns it streamed, so it needs a per-session push (reusing the ADR 0050
`background.completed` → `BackgroundWatch` pattern). That remains a follow-up
(tracked in beads); until then the work happens in-thread and surfaces on the
chat's next interaction.

## Consequences

- Long-horizon "do X, wait, do Y" tasks no longer burn a turn's recursion budget
  polling; the budget covers actual work.
- A `wait` issued inside a chat resumes in that same conversation thread — the
  agent keeps full context across the yield.
- `wait` is lead-agent-only (gated on the scheduler; subagents run bounded by
  `max_turns` and don't get it).
- The yield is durable across restart (the resume is a persisted scheduler job).
- Follow-up: live UI surfacing of the resumed turn in the originating chat tab
  (per-session push à la ADR 0050), so a watching user sees the continuation
  arrive rather than only the agent picking it up server-side.
