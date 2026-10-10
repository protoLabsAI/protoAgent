# Schedule future work

Ask the agent to run a task later or on a recurring schedule, for example:

> Every Monday at 9am in America/Los_Angeles, summarize the last week's pipeline incidents.

Open **Schedule** to confirm the saved prompt, timezone, and next fire time.
The server must be running when the job fires. Results appear in **Activity**.

## Manage from the console

Open **Schedule** to create a job, inspect its full prompt, edit its schedule, or
cancel it. Use a named timezone when local time matters; an unspecified timezone
uses UTC. Confirm the next fire time after saving.

Write a self-contained prompt: a scheduled run does not receive the conversation
that created it. Include the sources to read, the action to take, and the expected
output. For example, write “review last week's pipeline incidents and post a
summary,” rather than “do that thing we discussed.”

## Scheduling tools

When the scheduler is active, three tools land in `get_all_tools()`:

| Tool | What it does |
|---|---|
| `schedule_task(prompt, when, job_id?)` | Persist a future invocation. `when` is cron (`"0 9 * * *"`) or ISO-8601 (`"2026-05-01T15:00:00"`). |
| `list_schedules()` | Show all jobs visible to *this* agent. |
| `cancel_schedule(job_id)` | Remove a job by id. |

## Enabling / disabling

Scheduling is enabled by default. Set `middleware.scheduler: false` to disable
it durably, or use `SCHEDULER_DISABLED=1` as a process-level override. The scheduling
tools are unavailable while disabled.

## Operator API

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/api/scheduler/jobs` | List jobs (`{jobs, backend}`) |
| `POST` | `/api/scheduler/jobs` | Create: `{prompt, schedule, job_id?, timezone?}` |
| `PUT` | `/api/scheduler/jobs/{id}` | Partial update: `{prompt?, schedule?, timezone?}` |
| `DELETE` | `/api/scheduler/jobs/{id}` | Cancel |

An omitted field keeps its value. An explicit `timezone: null` or `""` switches
back to UTC. Invalid schedules or timezones return `400` without changing the job;
empty prompts, empty schedules, and updates with no fields also return `400`.

## Plugin-owned recurring jobs

A plugin arms its own cadence through the [consumption SDK](/guides/plugins#consumption-sdk)
(#1642) rather than asking the operator to wire a cron job:

```python
from graph import sdk

def register(registry):
    sdk.schedule_recurring(
        "Run the strategist OODA tick.", "0 9 * * *",
        plugin_id=registry.plugin_id, job_id="strategist-tick",
    )
```

- `sdk.schedule_recurring(prompt, cron, *, plugin_id, job_id, session="", timezone=None)`
  — a **recurring** cron cadence (one-shot turns stay on `sdk.run_in_session`; an ISO
  datetime is rejected here). Fires into the Activity thread by default; pass `session`
  to target a chat context. **Idempotent by id** — re-calling with the same `job_id`
  replaces the pending job, so `register()` can re-arm on every (re)load and a cadence
  knob change just re-schedules.
- `sdk.cancel_scheduled(job_id, *, plugin_id)` / `sdk.cancel_plugin_jobs(plugin_id)` —
  remove one cadence / all of them.

The job id is namespaced **`plugin:<plugin_id>:<job_id>`** — that ownership tag is what
lets the host clean up: **disabling** a plugin sweeps its `plugin:<id>:*` jobs on the
reload, and **uninstalling** sweeps them in the same pass that removes the code — no
orphan job keeps firing prompts about a plugin that's gone. Re-enabling relies on the
plugin re-arming in `register()` (which is why the idempotent-replace shape matters).
The `AGENT_NAME` scoping below is untouched — plugin ownership rides on the id *within*
an instance's jobs.db; it never crosses instances.

## Multi-agent isolation

Default schedules live in the instance-private
`<instance_root>/scheduler/agent/jobs.db`. Agent renaming keeps the same store;
an existing name-keyed store may be adopted on first access.

`SCHEDULER_DB_DIR` selects an explicit parent directory, with jobs stored at
`<override>/<agent_name>/jobs.db`. Avoid sharing that file between running instances:
the scheduler owner-lock allows only one process to poll it. Use separate
instance roots for independent agents; see [Run multiple instances](/guides/multi-instance).

## How firing works

The scheduler runs an asyncio polling task on FastAPI's `startup`
event. Once a second:

1. Read jobs where `next_fire <= now()` and `enabled = 1` (skipping any
   still firing — a slow turn won't be re-claimed mid-flight).
2. For each due job: POST to `http://127.0.0.1:<active_port>/a2a` as
   a `message/send` with the job's prompt as the message text, routed
   into the durable **Activity thread** (`contextId: system:activity`,
   `metadata.origin: scheduler`). Bearer + X-API-Key are forwarded
   automatically.
3. One-shot ISO jobs are deleted after firing. Cron jobs reschedule
   forward via `croniter` (advanced the instant they're claimed, so a
   long turn never double-fires).

Going through HTTP rather than calling into the graph directly buys
parity with real callers — the audit log, cost-v1 capture, and
push-notification path all behave identically.

**Where the response lands.** The fired turn runs in the Activity thread
(ADR 0003), so its output persists and shows up live in the console's
**Activity** surface (pushed over `/api/events` as an `activity.message`).

### Missed-fire recovery

On startup, jobs whose `next_fire` is in the past are inspected:

- **Within the last 24h** — fire on the next tick (so a 5-minute
  outage doesn't lose an upcoming reminder).
- **Older than 24h** — cron jobs roll forward to the next slot
  without firing; one-shot jobs are dropped. Avoids flooding the agent
  with stale prompts after a long downtime.

### Persistence path

Use `protoagent config explain` to find the instance root. In the bundled Docker
image, the default is `/sandbox/scheduler/agent/jobs.db`; on a normal host install,
it is `~/.protoagent/default/scheduler/agent/jobs.db`. Mount a volume covering the
instance root to preserve schedules across container restarts.

## Adding a case to your eval suite

The default `evals/tasks.json` doesn't include scheduler cases (the
fire path is async — a single eval run can't easily test that the
scheduled prompt arrives). For forks that want it, the pattern is:

1. `schedule_task(prompt, "<near-future ISO>")` in setup.
2. Wait > 1 second.
3. Assert on the audit log and/or KB state for the *fired* prompt's
   side effects.

Document the case as `category: "scheduler"` and gate at >= 2/3
attempts to absorb timing jitter.

## References

- [Configuration](/reference/configuration#scheduler) — env vars
- [Eval your fork](/guides/evals) — for the testing pattern above
