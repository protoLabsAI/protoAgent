# Goal mode

Give the agent a task and a completion check. After each turn, goal mode checks
the result and continues until the check passes or a stopping limit is reached.
Use it for outcomes the agent can work toward, such as passing tests or finishing
a document. Use a [watch](/guides/watches) for an external condition, such as a
deployment finishing.

## Set one (the short path)

1. Open **Goals → New goal** (or **+ Goal** in the Work overview).
2. Write a testable condition and choose a verifier. For a coding task, choose
   **test** and provide the test command and working directory.
3. Start the goal. It opens a dedicated chat where you can follow and steer the work.
4. Inspect the iteration count and verifier result in the Goals panel. Stop it
   when you want to end the run; use **Add iterations** if it needs more time.

For a goal judged from the conversation, send `/goal <condition>` in chat.
Command, test, CI, and expression verifiers must be created through the operator
panel or API; chat refuses them. The agent's `set_goal` tool uses registered
plugin verifiers. See [Setting a goal](#setting-a-goal) for exact forms.

A goal stops as `achieved`, `exhausted`, or `unachievable`. It can pause while
waiting for a watch, schedule, or delegated work and resume when the trigger
fires. Goal mode is available by default; its tuning lives in **Settings → Behavior**.

## How it works

After a turn, the verifier decides whether to finish or continue. Continuations
include its reason and evidence plus the saved plan. Each session has one goal.

| Result | Status or next action |
| --- | --- |
| Verifier passes | `achieved` |
| Iteration budget spent | `exhausted` |
| Unchanged progress reaches the no-progress limit | `unachievable` |
| Agent calls `abandon_goal` | `unachievable`, unless the verifier finds it already achieved |
| Pending watch or scheduled job targets this session | Pause automatic continuation; remain `active` |
| Per-turn model-round cap reached | Verify the turn, then pause if still unmet; remain `active` |

No-progress detection compares verifier reason/evidence for concrete checks; LLM
checks instead compare the saved plan, since evaluator wording can vary.

A watch or scheduled reaction must target the goal's session to resume that
work. A paused goal also resumes on the next message. A round-cap pause needs a
new message or trigger; it does not immediately start another capped turn.
See [goal configuration](/reference/configuration#goal) for the separate
iteration, no-progress, and per-turn limits.

## Setting a goal

Send a control message through any channel (A2A, the React console chat, OpenAI-compat):

- **Fuzzy goal** (LLM-verified):
  ```
  /goal the README documents every config block
  ```
- **Testable goal** (JSON spec) — from a chat message you can use the *declarative*
  verifiers (`plugin`, or `data` with a `contains` substring):
  ```
  /goal {"condition": "migration recorded", "verifier": {"type": "data", "path": "state.json", "contains": "migration complete"}}
  ```

  > **Shell/eval verifiers are operator-only.** Create `command`, `test`, `ci`,
  > and `data`+`expr` goals through **Goals → New goal** or `POST /api/goals`.
  > They are refused in a `/goal` chat message. The federation credential cannot
  > access the operator API; see [Security and trust](/explanation/security-and-trust).

- **Per-goal patience:** add `"no_progress_limit": N` to widen/narrow one goal's
  no-progress tolerance without changing the global default.
- **Status:** `/goal`
- **Clear:** `/goal clear` (aliases: `stop`, `off`, `cancel`, `reset`, `none`)

Type `/` in the chat composer to discover goal commands.

## Manage from the console

Open **Goals** to inspect each session's condition, status, iteration count, and
last verifier result. Select a row for its saved plan, completion contract,
evidence, and iteration timeline.

Use **Add iterations** to extend an active goal or **Restart** to re-arm a
finished one. When closing its chat tab, choose whether to keep it running in
the background or stop it. Stopping from that prompt also closes the tasks
filed in that session's backlog.

For authenticated endpoints, see [Goal API](/reference/automation#goal-api).

## Reacting to a goal

<!-- Preserve links to the former follow-up section. -->
<a id="goal-fires-→-run-a-follow-up-agent-turn"></a>

Plugins can react to completion or failure; see [goal hooks](/reference/automation#reacting-to-a-goal).

## Verifier types

Set via `verifier.type` in the JSON spec:

| Type | Spec keys | Met when |
|---|---|---|
| `command` | `command`, `cwd?`, `timeout?` | the shell command exits `0` |
| `test` | same as `command` | exits `0` (the runner's summary line is surfaced in the reason) |
| `ci` | `pr` **or** `branch` | `gh pr checks <pr>` is all-green, or the latest run on `branch` concluded `success` |
| `data` | `path` + (`contains` **or** `expr`), `workspace_relative?` | the file contains the substring, or `expr` (evaluated over parsed JSON as `data`) is truthy; the Goal/Watch forms and accepted chat specs set `workspace_relative` automatically |
| `plugin` | `check` (`<plugin-id>:<name>`) + `args?` | the plugin-registered verifier returns met — see [Plugins ▸ Goal & watch verifiers](/guides/plugins#goal-and-watch-verifiers) for `register_goal_verifier` and the `(spec, ctx)` contract (incl. `ctx.invoker`, the polling goal/watch's identity) |
| `llm` | — (uses `condition`) | a model judges the latest answer and recent tool summary against the condition |

`data` `expr` runs in a restricted namespace — the parsed document is `data`, with only read-only builtins (`len`, `any`, `all`, `sum`, …). `__import__`, `open`, `eval`, etc. are unavailable.

Examples:
```jsonc
{"type": "command", "command": "test -f /sandbox/out/report.pdf"}
{"type": "ci", "branch": "feat/my-branch"}
{"type": "data", "path": "/sandbox/state.json", "expr": "data['open_tickets'] == 0"}
```

## The running plan (`update_goal_plan`)

The agent records its plan with `update_goal_plan`. The controller saves it for
every goal and includes it in later continuations, including `fresh_context`
turns. The plan is also visible in the goal drawer. If the task cannot be
completed, `abandon_goal` records a reason; the verifier runs first so an already
satisfied goal still finishes as `achieved`. See
[ADR 0079](/adr/0079-autonomous-operating-model) for the durable work loop.

## Configuration

See the [`goal` config block](/reference/configuration#goal). Defaults: machinery `enabled`, `max_iterations: 8`, `no_progress_limit: 3`, `verify_timeout: 120`.

## Security

Command, test, CI, and `data` expression verifiers are operator-only: use the
Goals panel or authenticated API. Chat accepts `llm`, registered `plugin`
checks, and `data` substring checks; the agent's tool is restricted further to
plugin checks. See [Security and trust](/explanation/security-and-trust).

For chat-created data checks, use a workspace-relative path such as `report.md`.
Absolute paths and traversal are refused. The forms mark relative paths with
`workspace_relative: true`. Authenticated API specs without that flag retain
server-CWD-relative paths and support absolute paths for intentional checks
outside the managed workspace.
