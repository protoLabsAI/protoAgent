# Automation APIs and integration

Use [Automate](/guides/automate-work) to choose a mechanism and follow its app procedure. Operator API calls require an operator credential; federation credentials do not grant operator access. See [Operator API](/reference/operator-api) for authentication.

## Operator API

| Method | Path | Purpose |
| --- | --- | --- |
| `GET` | `/api/scheduler/jobs` | List jobs (`{jobs, backend}`) |
| `POST` | `/api/scheduler/jobs` | Create: `{prompt, schedule, job_id?, timezone?}` |
| `PUT` | `/api/scheduler/jobs/{id}` | Partial update: `{prompt?, schedule?, timezone?}` |
| `DELETE` | `/api/scheduler/jobs/{id}` | Cancel |

Omitted update fields keep their values. `timezone: null` or `""` resets to UTC.
Invalid schedules/timezones, empty prompts or schedules, and empty updates return
`400` without changing the job.

## Plugin-owned recurring jobs

Plugins use [`sdk.schedule_recurring`](/reference/plugin-sdk-api#sdk-schedule-recurring)
with a stable job id:

```python
from graph import sdk

def register(registry):
    sdk.schedule_recurring(
        "Review the project's outstanding tasks.", "0 9 * * *",
        plugin_id=registry.plugin_id, job_id="daily-review",
    )
```

Calling it again with the same id replaces the pending job. It accepts cron
only; use `sdk.run_in_session` for a one-shot turn. The default destination is
Activity; `session` targets a particular chat.

The host namespaces ids as `plugin:<plugin_id>:<job_id>` and removes owned jobs
on plugin disable/reload or uninstall. Re-enabling depends on the plugin
re-arming in `register()`. Plugins can also call `sdk.cancel_scheduled` or
`sdk.cancel_plugin_jobs`; see the SDK reference for their signatures.

## Goal API

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/api/goals` | List all goals across sessions (`{goals, enabled}`) |
| `GET` | `/api/goals/{session_id}` | One goal's detail + its `plan` artifact |
| `POST` | `/api/goals` | Set a goal (operator-tier — any verifier). `kick:false` drives it from a chat tab instead of a headless turn |
| `POST` | `/api/goals/{session_id}/rearm` | Extend an active goal's budget, or restart a terminal one |
| `POST` | `/api/goals/{session_id}/resume` | Keep an active goal running headlessly (detach on tab close) |
| `DELETE` | `/api/goals/{session_id}` | Clear (stop) one (`{cleared, tasks_closed}`); `?close_tasks=true` also closes its task backlog |

The agent's `set_goal` tool accepts registered plugin verifiers only. Plugins
can drive a goal loop through
[`sdk.start_goal_loop`](/reference/plugin-sdk-api#sdk-start-goal-loop).

## Reacting to a goal

Plugins can subscribe to `goal.achieved` or `goal.failed` on the
[event bus](/guides/plugins#event-bus). The failed event covers `exhausted` and
`unachievable`; both events carry
`{session_id, condition, status, reason, evidence}`.

For the full terminal goal state, use
`registry.register_goal_hook(on_achieved=…, on_failed=…)`.

### Goal fires → run a follow-up agent turn

A hook can enqueue a one-shot turn in the completed goal's session:

```python
from graph import sdk

def register(registry):
    async def on_achieved(goal):
        sdk.run_in_session(
            goal.session_id,
            f"Summarize the completed goal: {goal.condition}. "
            f"Evidence: {goal.last_evidence}",
        )
    registry.register_goal_hook(on_achieved=on_achieved)
```

`run_in_session` returns without waiting for the turn. Its `job_id` makes
scheduling idempotent; `delay_seconds` defers it. See the
[SDK reference](/reference/plugin-sdk-api#sdk-run-in-session) for the contract.

## Watch creation API

| Creation path | Verifier and lifetime inputs |
| --- | --- |
| Agent `create_watch` | Plugin `check` name and optional `check_args`; `expires_in_s` is relative to now; reaction runs in the creating session |
| Plugin `sdk.create_watch` | Plugin name in `verifier`, optional `verifier_args`; epoch `deadline`; set both `run_prompt` and `run_session` for a reaction turn |
| Operator `POST /api/watches` | Any [verifier spec](/guides/goal-mode#verifier-types); absolute `deadline` as ISO-8601 or epoch; optional reaction prompt/session |

The operator API also provides `GET /api/watches`, `PATCH /api/watches/{id}`,
and `DELETE /api/watches/{id}`. Shell, test, CI, data, and LLM verifiers are
operator-only. For example, an authenticated operator can post:

```json
{
  "condition": "rollout complete",
  "interval_s": 60,
  "run_prompt": "Run the smoke test and report.",
  "run_session": "ops",
  "verifier": {
    "type": "command",
    "command": "kubectl rollout status deploy/api",
    "timeout": 30
  }
}
```

Add an absolute `deadline` if this watch should expire. This command runs on the
server host, which needs `kubectl` and access to the cluster.

## Background work

**Reuse it from your own code — `BackgroundManager.spawn_work(...)`.** Not every long job is an
LLM turn. A *deterministic* pipeline — media transcription, a bulk import, a crawl — shouldn't
spend a model turn on itself. `spawn_work` runs a plain coroutine through the **same** durable
registry + concurrency cap + `background.*` event stream + drain-on-next-turn + idle-wake as a
background subagent, with no LLM turn:

```python
from runtime.state import STATE

mgr = STATE.background_mgr  # None when the background subsystem is disabled → run inline instead
if mgr is not None:
    job_id = await mgr.spawn_work(
        origin_session=session_id,
        kind="ingest",                     # short label — shows on the job card
        description="Ingest that video",
        detail=source,                     # recorded on the job row
        work=lambda: do_the_work(source),  # an async () -> result-string callable
    )
```

The `knowledge_ingest` tool is the first consumer — it detaches any slow URL/media ingest this
way so a 20-minute transcription never freezes the chat. Reach for `spawn_work` whenever a tool
or plugin has a long **deterministic** operation that shouldn't block the turn (vs.
`run_in_background`, which is for detaching an *LLM subagent* turn).

**From a plugin, use the consumption SDK instead of `STATE`.** `graph.sdk.spawn_background(prompt,
*, subagent_type, origin_session, label=None)` spawns a detached *subagent* job through the same
pipeline — the report card, KB indexing, and the push-resume nudge all land for free — and
`graph.sdk.background_status(task_id)` reads the job row (`{status, description, report?}`) so a
plugin dashboard can show progress between launch and the nudge. See
[Plugins ▸ Tapping core deeper](/guides/plugins#consumption-sdk).

