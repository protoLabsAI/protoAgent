# Goal mode

A **goal** is a testable outcome you attach to the agent — a *condition* plus a **verifier** that ground-truths whether it's met (a shell command's exit code, a test run, a CI status, a data assertion, a plugin check, or an LLM judgment as the fallback). Goals turn "please do X" into "keep going / watch until X is provably true."

There are **two kinds**, and the difference is *who moves the needle*:

- **Drive** (default) — *the agent's own turns* do the work. After each turn the verifier runs; if not met, the agent is re-invoked with a continuation prompt until it passes, the iteration budget is spent, or it's flagged unachievable. Use for "make the tests pass," "finish the README."
- **Monitor** (ADR 0030) — *an external process* moves the needle (a background engine, a training run, a deployment, a market). The agent only starts/supervises; the goal is checked **out-of-band on a cadence** and never re-invokes the agent. Use for "treasury ≥ 1,000,000," "rollout reaches 100%."

When a goal reaches a terminal state it **broadcasts on the event bus** (`goal.achieved` / `goal.failed`, ADR 0039) — so the console, or any plugin, can react without writing code (see [Reacting to a goal](#reacting-to-a-goal)).

It's modelled on protocli's goal system but deliberately more rigorous for a long-running server agent:

| | protocli | protoAgent goal mode |
|---|---|---|
| Completion check | small-LLM judgment | **pluggable verifier** (command / test / CI / data), LLM only as fallback |
| Drive-to-done | continuation prompt | continuation prompt **+ persisted `<goal_plan>` checklist** |
| Give-up path | user sets "stop after N" in the text | **iteration budget + no-progress streak + model `<goal_unachievable>`** |
| State | in-memory, per session | **disk-persisted** per session (survives restart/reload) |

## How it works

1. You set a goal for a session (`/goal …`). Nothing else changes — the next message runs normally.
2. When the agent produces a final answer (no more tool calls), the controller runs the goal's **verifier**.
3. **Met** → the goal is marked `achieved` and the run ends.
4. **Not met** → the controller extracts/refreshes the agent's `<goal_plan>` checklist, then re-invokes the agent on the same thread (history preserved) with a continuation prompt that includes the verifier's reason + evidence and the current plan.
5. This repeats until met, the **iteration budget** (`goal.max_iterations`) is spent (`exhausted`), the verifier returns the **same evidence too many times** (`goal.no_progress_limit` → `unachievable`), or the agent itself emits `<goal_unachievable reason="…"/>` (`unachievable`).

The loop wraps graph invocation in `server/chat.py` (both the A2A streaming path and the non-streaming chat path); the graph itself is unchanged.

## Setting a goal

Send a control message through any channel (A2A, the React console chat, OpenAI-compat):

- **Fuzzy goal** (LLM-verified):
  ```
  /goal the README documents every config block
  ```
- **Testable goal** (JSON spec):
  ```
  /goal {"condition": "unit tests pass", "verifier": {"type": "test", "command": "python -m pytest -q"}}
  ```
- **Monitor goal** (ADR 0030) — for a metric driven by an *external* process (a
  background engine, a training run, a deployment), not the agent's turns. Add
  `"mode": "monitor"`: the agent **isn't** re-invoked, the goal **never exhausts**,
  and it's checked **out-of-band** on a cadence (`goal.monitor_interval`, default 60s),
  firing the verifier's `on_achieved` hook when it passes.
  ```
  /goal {"condition": "treasury ≥ 1,000,000", "mode": "monitor", "verifier": {"type": "plugin", "check": "spacetraders:credits", "args": {"min": 1000000}}}
  ```
  (Default is `"mode": "drive"` — the agent *is* the work, the bounded loop above.)
- **Per-goal patience:** add `"no_progress_limit": N` to widen/narrow one goal's
  no-progress tolerance without changing the global default.
- **Status:** `/goal`
- **Clear:** `/goal clear` (aliases: `stop`, `off`, `cancel`, `reset`, `none`)

In the React console, typing `/` in the chat composer opens a command
autocomplete (served from `GET /api/chat/commands`) so `/goal` is discoverable;
↑/↓ to pick, Enter/Tab to insert.

Programmatic status/clear is also available: `GET /api/goal/{session_id}` and `DELETE /api/goal/{session_id}`.

## Manage from the console

The React console's **Goals** surface (right sidebar) lists every session's goal — its condition, status (`active` / `achieved` / `exhausted` / `unachievable`), a **`monitor` badge** for monitor goals, the verifier type, and either the drive **iteration count** or (for monitor) **when it was last checked** — plus the latest verifier reason. You can **clear** any of them. When a goal finishes, the console shows a **toast** (`goal.achieved` → success, `goal.failed` → error), driven by the bus events below.

Goals are still *set* in chat with `/goal` (setting can run shell/test verifiers, so it stays an explicit operator action); the panel is a read-and-clear view. Backed by:

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/api/goals` | List all goals across sessions (`{goals, enabled}`) |
| `POST` | `/api/goals` | Set a goal **programmatically** — `plugin` verifier only (safe set; command/test/ci/data/llm stay operator-only via `/goal`) |
| `DELETE` | `/api/goals/{session_id}` | Clear one (`{cleared}`) |

## Reacting to a goal

A terminal goal is a **trigger**, not just a checkbox. Every finish publishes one of two events on the [event bus](/guides/plugins#events-the-plugin-bus) (ADR 0039):

| Topic | When | Payload |
|---|---|---|
| `goal.achieved` | verifier passed | `{session_id, condition, status, reason, evidence, mode}` |
| `goal.failed` | `exhausted` / `unachievable` | same shape |

Two ways to react:

- **No code (any plugin / the console).** Subscribe to the topic — `registry.on("goal.achieved", …)` in a plugin, or `protoagent:subscribe` from a sandboxed view. The built-in console toast is exactly this. Because it's the bus, **nobody imports the goal system** to listen.
- **Plugin code (richer).** `register_goal_hook(on_achieved=…, on_failed=…)` hands your plugin the terminal `GoalState` to run arbitrary logic — set the next goal (phase progression), kick a follow-up agent turn via `host.invoke`, stop a background engine, alert. This is how a fork drives an autonomous loop: *set a monitor goal → external engine runs → the cadence tick verifies → the hook advances.*

## Verifier types

Set via `verifier.type` in the JSON spec:

| Type | Spec keys | Met when |
|---|---|---|
| `command` | `command`, `cwd?`, `timeout?` | the shell command exits `0` |
| `test` | same as `command` | exits `0` (the runner's summary line is surfaced in the reason) |
| `ci` | `pr` **or** `branch` | `gh pr checks <pr>` is all-green, or the latest run on `branch` concluded `success` |
| `data` | `path` + (`contains` **or** `expr`) | the file contains the substring, or `expr` (evaluated over parsed JSON as `data`) is truthy |
| `llm` | — (uses `condition`) | a strict evaluator judges the transcript shows the goal demonstrably done (fuzzy fallback) |

`data` `expr` runs in a restricted namespace — the parsed document is `data`, with only read-only builtins (`len`, `any`, `all`, `sum`, …). `__import__`, `open`, `eval`, etc. are unavailable.

Examples:
```jsonc
{"type": "command", "command": "test -f /sandbox/out/report.pdf"}
{"type": "ci", "branch": "feat/my-branch"}
{"type": "data", "path": "/sandbox/state.json", "expr": "data['open_tickets'] == 0"}
```

## The `<goal_plan>` checklist

Continuation prompts ask the agent to keep a running plan inside a `<goal_plan>…</goal_plan>` block and update it each turn. The controller extracts that block, persists it with the goal state, and feeds it back into the next continuation — so the agent maintains a coherent plan across iterations instead of re-planning from scratch.

## Configuration

See the [`goal` config block](/reference/configuration#goal). Defaults: machinery `enabled`, `max_iterations: 8`, `no_progress_limit: 3`, `verify_timeout: 120`.

## Security

`command` / `test` / `ci` verifiers execute on the server host with the agent's privileges. **Setting a goal is an operator action** — only accept goal specs from trusted callers. If you expose `/goal` to untrusted input, restrict it to `data` / `llm` verifiers or gate goal-setting behind auth.
