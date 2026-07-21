# Goal mode

A **goal** is a testable outcome you attach to the agent — a *condition* plus a **verifier** that ground-truths whether it's met (a shell command's exit code, a test run, a CI status, a data assertion, a plugin check, or an LLM judgment as the fallback). Goals turn "please do X" into "keep going / watch until X is provably true."

A goal is **agent-driven**: *the agent's own turns* do the work. After each turn the verifier runs; if not met, the agent is re-invoked with a continuation prompt until it passes, the iteration budget is spent, or it's flagged unachievable. Use for "make the tests pass," "finish the README."

> **Watching a metric someone else moves** (a background engine, a training run, a deploy — "treasury ≥ 1,000,000," "rollout reaches 100%") is a **watch**, not a goal (ADR 0067): it's checked out-of-band on a cadence, never re-invokes the agent, and you can hold **many** at once. Create one with `sdk.create_watch(...)`, `POST /api/watches`, or the agent's `create_watch` tool. (Goals used to carry a `monitor` disposition; ADR 0067 split it into its own primitive.)

When a goal reaches a terminal state it **broadcasts on the event bus** (`goal.achieved` / `goal.failed`, ADR 0039) — so the console, or any plugin, can react without writing code (see [Reacting to a goal](#reacting-to-a-goal)).

> Goal mode is **always on** — there's no enable/disable toggle. The machinery stays dormant (and the `set_goal` tool a no-op gate) until you actually set a goal, so it costs nothing when unused. The tuning knobs (`goal.max_iterations`, `goal.eval_model`) live in **Settings ▸ Agent**.

It's modelled on protocli's goal system but deliberately more rigorous for a long-running server agent:

| | protocli | protoAgent goal mode |
|---|---|---|
| Completion check | small-LLM judgment | **pluggable verifier** (command / test / CI / data), LLM only as fallback |
| Drive-to-done | continuation prompt | continuation prompt **+ a persisted plan** (the `update_goal_plan` tool) |
| Give-up path | user sets "stop after N" in the text | **iteration budget + no-progress streak + the `abandon_goal` tool** |
| State | in-memory, per session | **disk-persisted** per session (survives restart/reload) |

## How it works

1. You set a goal for a session (`/goal …`). Nothing else changes — the next message runs normally.
2. When the agent produces a final answer (no more tool calls), the controller runs the goal's **verifier**.
3. **Met** → the goal is marked `achieved` and the run ends.
4. **Not met** → the agent records its running plan with the `update_goal_plan` tool, then the controller re-invokes it on the same thread (history preserved) with a continuation prompt that includes the verifier's reason + evidence and the current plan.
5. This repeats until met, the **iteration budget** (`goal.max_iterations`) is spent (`exhausted`), the verifier returns the **same evidence too many times** (`goal.no_progress_limit` → `unachievable`), or the agent itself calls the `abandon_goal` tool with a reason (`unachievable`).

The loop wraps graph invocation in `server/chat.py` (both the A2A streaming path and the non-streaming chat path); the graph itself is unchanged.

**Yield instead of spin (ADR 0079).** If the agent's next step waits on async or delegated work — a build, a peer agent, CI, a review — it doesn't have to burn iterations polling. It hands off to a [watch](/guides/watches) or a [schedule](/guides/scheduler) and ends the turn; the drive **pauses** (the goal stays `active`, iterations untouched) and **resumes automatically** when the trigger fires (`⏸ goal paused — handed off to a watch/schedule`). This is what lets a long, delegated goal span time instead of exhausting its budget waiting. Goals, tasks, watches, and schedules compose into one OODA loop over the agent's durable working-state — see [ADR 0079](/adr/0079-autonomous-operating-model).

## Setting a goal

Send a control message through any channel (A2A, the React console chat, OpenAI-compat):

- **Fuzzy goal** (LLM-verified):
  ```
  /goal the README documents every config block
  ```
- **Testable goal** (JSON spec) — from a chat message you can use the *declarative*
  verifiers (`plugin`, or `data` with a `contains` substring):
  ```
  /goal {"condition": "migration recorded", "verifier": {"type": "data", "path": "/sandbox/state.json", "contains": "migration complete"}}
  ```

  > **Shell/eval verifiers are operator-only.** `command`, `test`, `ci`, and `data`+`expr`
  > execute on the host or hit a restricted-eval sink, so they are **refused from a `/goal`
  > chat message** (a federation peer / API client shares the operator bearer today, #1407).
  > A dedicated operator set-channel is the Phase 2 plan.
  (To *watch* a metric an external process moves — "treasury ≥ 1,000,000", "rollout
  reaches 100%" — use a **watch** (ADR 0067), not a goal: `POST /api/watches` or the
  `create_watch` tool. Watches poll out-of-band, react via `run_in_session`/hooks, support
  `deadline`/`stall_after`, and you can hold many at once.)
- **Per-goal patience:** add `"no_progress_limit": N` to widen/narrow one goal's
  no-progress tolerance without changing the global default.
- **Status:** `/goal`
- **Clear:** `/goal clear` (aliases: `stop`, `off`, `cancel`, `reset`, `none`)

In the React console, typing `/` in the chat composer opens a command
autocomplete (served from `GET /api/chat/commands`) so `/goal` is discoverable;
↑/↓ to pick, Enter/Tab to insert.

Programmatic status/clear is also available: `GET /api/goals/{session_id}` and `DELETE /api/goals/{session_id}`.

## Manage from the console

The React console's **Goals** surface (right sidebar, in the Work hub) lists every session's goal — its condition, status (`active` / `achieved` / `exhausted` / `unachievable`), the verifier type, the **iteration count**, and the latest verifier reason. When a goal finishes, the console shows a **toast** (`goal.achieved` → success, `goal.failed` → error), driven by the bus events below.

**Create** — the panel's **New goal** action (and the Work overview's *+ Goal* quick-add) open a guided wizard: the condition, a type-aware verifier picker, and an optional completion contract (ADR 0073). A goal created here **opens a dedicated, focused chat tab and drives in it**, so the whole loop streams live; a goal set in chat with `/goal` stays in that chat. Command/test/ci/data verifiers are allowed here — the trust gate that refuses them from a raw `/goal` chat message doesn't apply to the authenticated, operator-tier `/api/goals` path (ADR 0066).

**Inspect** — click a row to open the **detail drawer**: the agent's live **plan** (`.plan.md`, rendered as markdown), the completion-contract read-back, the last verifier reason/evidence, and a per-iteration **timeline** (ADR 0079).

**Steer** — from the drawer, give an active goal more room (**Add iterations**) or **Restart** a terminal one (re-arm + resume). Closing a goal's chat tab prompts you: keep it **running in the background** (detach), or **stop** it — which clears the goal and closes the tasks it filed (its session-scoped backlog, ADR 0079).

Backed by:

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/api/goals` | List all goals across sessions (`{goals, enabled}`) |
| `GET` | `/api/goals/{session_id}` | One goal's detail + its `plan` artifact |
| `POST` | `/api/goals` | Set a goal (operator-tier — any verifier). `kick:false` drives it from a chat tab instead of a headless turn |
| `POST` | `/api/goals/{session_id}/rearm` | Extend an active goal's budget, or restart a terminal one |
| `POST` | `/api/goals/{session_id}/resume` | Keep an active goal running headlessly (detach on tab close) |
| `DELETE` | `/api/goals/{session_id}` | Clear (stop) one (`{cleared, tasks_closed}`); `?close_tasks=true` also closes its task backlog |

> The `plugin`-verifier-only **safe set** (`sdk.set_goal_safe`, ADR 0028 D3) is a separate, programmatic path for agents/plugins — distinct from the operator `/api/goals` route above.

## Reacting to a goal

A terminal goal is a **trigger**, not just a checkbox. Every finish publishes one of two events on the [event bus](/guides/plugins#event-bus) (ADR 0039):

| Topic | When | Payload |
|---|---|---|
| `goal.achieved` | verifier passed | `{session_id, condition, status, reason, evidence, mode}` |
| `goal.failed` | `exhausted` / `unachievable` | same shape |

Two ways to react:

- **No code (any plugin / the console).** Subscribe to the topic — `registry.on("goal.achieved", …)` in a plugin, or `protoagent:subscribe` from a sandboxed view. The built-in console toast is exactly this. Because it's the bus, **nobody imports the goal system** to listen.
- **Plugin code (richer).** `register_goal_hook(on_achieved=…, on_failed=…)` hands your plugin the terminal `GoalState` to run arbitrary logic — set the next goal (phase progression), **prompt the agent with a follow-up turn** (`sdk.run_in_session`, below), stop a background engine, alert. This is how a plugin drives an autonomous loop: *a terminal goal fires the hook → set the next goal, or prompt the agent.* (To watch an external metric on a cadence, use a **watch** instead — ADR 0067.)

### Goal fires → run a follow-up agent turn

To have the agent *act* when a goal fires — not just record a status — call `sdk.run_in_session(session_id, prompt)` from a hook. It enqueues the prompt as a **one-shot agent turn in the goal's own session** (that session's memory + full tools), runs it on the normal scheduler fire path, and returns immediately — so it's safe to call from a hook without blocking:

```python
from graph import sdk

def register(registry):
    async def on_achieved(goal):                # terminal GoalState
        sdk.run_in_session(
            goal.session_id,
            f"The goal '{goal.condition}' just completed. Evidence: {goal.last_evidence}. "
            f"Summarize the outcome and start the follow-up work.",
        )
    registry.register_goal_hook(on_achieved=on_achieved)
```

Pass `job_id=` to make the re-arm idempotent, or `delay_seconds=` to defer the turn. This is the *reaction* half of the self-improving loop: a recurring cadence **drives** the work (`sdk.start_goal_loop` arms one against a watch-verified target — ADR 0067/#2060); a hook + `run_in_session` **reacts** when it lands.

## Verifier types

Set via `verifier.type` in the JSON spec:

| Type | Spec keys | Met when |
|---|---|---|
| `command` | `command`, `cwd?`, `timeout?` | the shell command exits `0` |
| `test` | same as `command` | exits `0` (the runner's summary line is surfaced in the reason) |
| `ci` | `pr` **or** `branch` | `gh pr checks <pr>` is all-green, or the latest run on `branch` concluded `success` |
| `data` | `path` + (`contains` **or** `expr`) | the file contains the substring, or `expr` (evaluated over parsed JSON as `data`) is truthy |
| `plugin` | `check` (`<plugin-id>:<name>`) + `args?` | the plugin-registered verifier returns met — see [Plugins ▸ Goal & watch verifiers](/guides/plugins#goal-and-watch-verifiers) for `register_goal_verifier` and the `(spec, ctx)` contract (incl. `ctx.invoker`, the polling goal/watch's identity) |
| `llm` | — (uses `condition`) | a strict evaluator judges the transcript shows the goal demonstrably done (fuzzy fallback) |

`data` `expr` runs in a restricted namespace — the parsed document is `data`, with only read-only builtins (`len`, `any`, `all`, `sum`, …). `__import__`, `open`, `eval`, etc. are unavailable.

Examples:
```jsonc
{"type": "command", "command": "test -f /sandbox/out/report.pdf"}
{"type": "ci", "branch": "feat/my-branch"}
{"type": "data", "path": "/sandbox/state.json", "expr": "data['open_tickets'] == 0"}
```

## The running plan (`update_goal_plan`)

Continuation prompts ask the agent to keep a running plan and record it each turn by calling the **`update_goal_plan`** tool. The controller persists that plan to a durable plan artifact for **every** goal and feeds it back into the next continuation — so the agent maintains a coherent plan across iterations instead of re-planning from scratch. (ADR 0079 unified this: the plan used to be written durably only for `fresh_context` goals, so a default same-session goal maintained a plan that `read_plan()` never saw.) The plan is injected back each turn as part of the agent's `<working_state>` block, and it doubles as the **`orient`** signal in the [fleet trace export](/adr/0079-autonomous-operating-model): a goal that maintains a real plan emits `loop_shape=ooda` training rows; a goal with no plan is labelled `react`. To stop early when the goal is impossible or out of scope, the agent calls **`abandon_goal`** with a reason (honoured only after the verifier runs, so a goal the world already satisfies still finishes `achieved`). Both tools are bound whenever goal mode is on and are harmless no-ops outside a goal.

## Configuration

See the [`goal` config block](/reference/configuration#goal). Defaults: machinery `enabled`, `max_iterations: 8`, `no_progress_limit: 3`, `verify_timeout: 120`.

## Security

`command` / `test` / `ci` verifiers execute on the server host with the agent's privileges. **Setting a goal is an operator action** — only accept goal specs from trusted callers. If you expose `/goal` to untrusted input, restrict it to `data` / `llm` verifiers or gate goal-setting behind auth.

> **`data`-verifier path must sit inside the agent's writable workspace when the goal is agent-completed over untrusted `/goal`.** The agent's `read_file`/`write_file` tools are rooted at its `workspace` project (`/sandbox/workspace/`) and **cannot reach the parent** — `../x` → *"path escapes project 'workspace'"* — and the shell fallback that could write elsewhere is **declined** for an untrusted caller (no operator present to approve it). So a verifier pointed at `/sandbox/report.md` will never flip: the agent writes to `/sandbox/workspace/report.md` and the verifier reads a path it can't produce. Point the `path` at `/sandbox/workspace/…`. (This is by design — the deterministic verifier, not the agent's own "done" self-report, is the sole arbiter, so a mispathed artifact correctly leaves the goal unverified.)
