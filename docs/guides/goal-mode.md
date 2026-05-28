# Goal mode

Goal mode gives the agent a **testable outcome** and lets it self-drive toward it: after every turn the agent stops on, a **verifier** checks whether the goal is met; if not, the agent is re-invoked with a continuation prompt until it passes, runs out of budget, or is flagged unachievable.

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

The loop wraps graph invocation in `server.py` (both the A2A streaming path and the non-streaming chat path); the graph itself is unchanged.

## Setting a goal

Send a control message through any channel (A2A, Gradio chat, OpenAI-compat):

- **Fuzzy goal** (LLM-verified):
  ```
  /goal the README documents every config block
  ```
- **Testable goal** (JSON spec):
  ```
  /goal {"condition": "unit tests pass", "verifier": {"type": "test", "command": "python -m pytest -q"}}
  ```
- **Status:** `/goal`
- **Clear:** `/goal clear` (aliases: `stop`, `off`, `cancel`, `reset`, `none`)

Programmatic status/clear is also available: `GET /api/goal/{session_id}` and `DELETE /api/goal/{session_id}`.

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
