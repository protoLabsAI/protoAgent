# 0079 — Autonomous operating model (goals · tasks · scheduling · watches → one OODA loop)

- Status: Accepted
- Date: 2026-07-08
- Builds on: ADR 0028 (plugin goal verifiers), ADR 0030 (monitor goals — superseded by 0067),
  ADR 0053 (`wait`/`run_in_session` resume), ADR 0067 (standalone watch primitive), ADR 0073
  (goal completion contracts), ADR 0074 (system lifecycle events).
- Supersedes the loose ends of: the plan-storage half of the goal subsystem, and the
  "primitives compose" intent gestured at across 0030/0067/0073 but never wired.

## Context

An agent has four primitives for acting over time — **goals** (`graph/goals/`), **tasks**
(the `task_*` board), **scheduling** (`scheduler/`), and **watches** (`graph/watches/`).
Together they are, in principle, everything an agent needs to run itself: hold an objective,
break it into work, manage timing and external conditions, and self-correct. In practice they
are **four disconnected silos with no shared operating model**, and the agent is never told
they compose. A due-diligence pass across all four subsystems (and a live fleet dogfood) found:

1. **No operating doctrine exists.** The system prompt is persona (`SOUL.md`) plus six tactical
   bullets (`graph/prompts.py`). The only long-horizon primitive it names is `wait()`. The five
   primitives are documented *only* in isolated tool docstrings; nothing tells the agent to hold
   a goal, decompose it into tasks, or schedule/watch for timing. The operating-model block in
   `prompts.py` is a `# OVERRIDE THIS in your fork` placeholder that was never filled in.

2. **The agent cannot observe its own commitments.** No middleware injects the active goal, open
   tasks, live watches, or pending schedules into context. The agent sees them only if it
   remembers to poll `task_list`/`list_watches`/`list_schedules` — which nothing prompts. The
   "Observe" step of OODA has nothing to observe about the agent's *own* state.

3. **The primitives have no agent-facing bridge.** The only composition primitive,
   `run_in_session`, is plugin/SDK-only. The agent has no way to make a goal schedule a
   follow-up, a watch advance a goal, or a task become a trigger. The task board is inert —
   nothing polls it.

4. **The goal "plan" is split-brained.** `record_plan` writes `state.checklist` for a default
   (same-session) goal but the durable `.plan.md` artifact only for `fresh_context` goals. The
   continuation loop-back and the trace-export "orient"/`loop_shape` signal read only `.plan.md`.
   So a default goal maintains a plan that `read_plan()` never sees → the turn is always labelled
   `react`. **Live proof:** a fleet PM agent drove a goal for 11.8 min maintaining a 542-char
   plan and every one of its 11 trace rows was `react`, `orient_len=0`. The fleet emits **0%**
   of the OODA signal the lab keys off (`observability/trace_export.py`).

5. **A long, delegated goal cannot span time.** The goal drive is a bounded, *synchronous*
   re-invoke loop. The same dogfood agent delegated an async multi-agent build, then burned its
   8-iteration budget waiting and gave up (`exhausted`, no deliverable) — because it had no way
   to hand the async work off to a watch/schedule and resume. The primitives that would have let
   it self-manage across time exist; it was never told they compose and cannot reach them as a
   loop.

The through-line: **"OODA" is only a post-hoc trace label, never a prompted behavior.** We
measure a loop we never taught the agent to run.

## Decision

Define a single **autonomous operating model** and make it real in the prompt and the wiring.

### The model

The agent's **durable working-state** is:

> **{ active goal + its plan (orient) · open tasks · live watches · pending schedules }**

The agent runs an **OODA loop** over that state:

- **Observe** — every turn, the agent is *shown* its working-state (injected, not polled) plus
  why it is awake (a scheduled fire / a watch trip / an operator turn).
- **Orient** — it maintains a durable plan and decomposes the goal into tracked tasks; the plan
  is the world-model, the task board is the backlog.
- **Decide** — it picks the next concrete step, and decides whether to act now, schedule a
  follow-up, or set a watch on an external condition.
- **Act** — it does the work (directly or by delegation) and, for anything async, **hands off to
  a schedule/watch and yields** instead of spinning; when the trigger fires it resumes with
  context. The deterministic verifier remains the sole arbiter of DONE (ADR 0073).

### Five moves

1. **Unify the plan store.** `record_plan` always writes the durable `.plan.md`; `state.checklist`
   is removed (no back-compat). `read_plan()` — and therefore the continuation loop-back and the
   `orient`/`loop_shape` signal — works for **every** goal. The non-`fresh_context` kickoff prompt
   asks for a plan too (it was silent). Root fix for the 0% OODA finding, at the source rather
   than by patching the exporter.

2. **Observe: inject `<working_state>`** each turn (active goal + plan, open tasks, live watches,
   pending schedules), bounded and empty-safe, via the `# Context` injection point
   (`graph/middleware/knowledge.py`). Injected on goal turns too.

3. **Write the doctrine** into the system prompt (`graph/prompts.py`): the OODA loop over
   working-state and how the five primitives compose. Extend the goal kickoff/continuation
   prompts to point at `task_create`/`schedule_task`/`create_watch`, not just `update_goal_plan`.
   `SOUL.md` stays pure persona.

4. **Compose + async handoff.** Agent-facing bridges: tasks carry a goal/session reference; the
   goal drive can yield to a schedule/watch and resume; scheduled/watch fires carry "why am I
   awake" (a distinct watch origin + the originating goal/condition/evidence) so the agent
   orients on wake instead of receiving a bare prompt.

5. **Trace alignment.** With move 1 the label is truthful; verify real OODA rows flow to the lab.

### Non-goals / invariants

- **No LLM judge in the reward path.** Reward stays deterministic terminal-state; the verifier
  stays the sole DONE arbiter (ADR 0073). The operating model shapes *behavior*, never *reward*.
- **No back-compat / migration.** `state.checklist` and the `fresh_context` plan-storage fork are
  deleted outright (`fresh_context` keeps its *thread-isolation* behavior — only the plan-storage
  fork goes). Existing on-disk goals re-plan on their next turn; acceptable.
- **Prod safety.** Changes land in `protoAgent` behind the full gate suite and are validated on the
  dev sandbox; the running fleet only picks them up on a deliberate image rebuild.

## Consequences

- **Good:** goals emit real OODA traces; the agent can see and drive its own commitments; long,
  delegated goals self-manage across time instead of exhausting; the four primitives become one
  coherent loop; the trace label measures a behavior we actually taught.
- **Cost:** a larger, always-on `# Context` block (bounded); a real system-prompt doctrine to
  maintain; the goal drive gains a yield/resume path (more states to test).
- **Rollout:** staged P0→P4 (plan-store unification → Observe injection → doctrine → composition →
  trace verification), each independently tested, one PR, gates green, dev-validated before any
  fleet roll.

### Deferred (safe follow-up)

- **Durable task→goal attribution** (a `session_id`/`goal_id` column on the task board) is
  deferred to its own change. The task board is instance-global and holds live prod data, so its
  schema migration is verified separately rather than folded into this PR. The composition itself
  is already delivered behaviorally: `<working_state>` surfaces the goal and the open tasks
  together every turn, and the doctrine + goal-drive tactic instruct the agent to decompose the
  goal into `task_create` items — so goals and tasks compose in the loop today; only the durable
  back-reference (for console filtering/attribution) waits.
