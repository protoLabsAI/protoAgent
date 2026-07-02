# Schedule future work

protoAgent ships a scheduler so the agent can defer tasks to itself —
"remind me about X tomorrow", "every Monday morning summarize last
week's logs", "at 3pm check the deploy". The bundled `LocalScheduler`
(sqlite + asyncio) is the one backend.

## When to read this

- You want forks (or your own multiple agents) to support reminders,
  recurring sweeps, or any "do this later" intent.
- You're spinning up multiple protoAgent instances on one box and
  need scheduling state to stay isolated per agent.

## The three tools

When the scheduler is active, three tools land in `get_all_tools()`:

| Tool | What it does |
|---|---|
| `schedule_task(prompt, when, job_id?)` | Persist a future invocation. `when` is cron (`"0 9 * * *"`) or ISO-8601 (`"2026-05-01T15:00:00"`). |
| `list_schedules()` | Show all jobs visible to *this* agent. |
| `cancel_schedule(job_id)` | Remove a job by id. |

Prompts are self-contained — the agent has no memory of the
scheduling moment when the task fires, so write the prompt as a fresh
turn ("review last week's pipeline incidents and post a summary",
not "do that thing we discussed").

## Enabling / disabling

`server/agent_init.py::_build_scheduler` builds the bundled
`LocalScheduler` (sqlite, asyncio polling) at startup unless scheduling
is turned off:

1. `middleware.scheduler: false` in YAML → no scheduler. The three
   tools don't ship. (Symmetric with `middleware.knowledge` /
   `middleware.memory` — drawer/wizard editable, survives restarts.)
   This is the canonical opt-out.
2. `SCHEDULER_DISABLED=1` env → no scheduler. Runtime escape hatch
   for fleet operators who can't edit config in the moment.

The scheduler is **default on** — opt out via either path above for a
stateless agent.

```bash
python -m server   # LocalScheduler on by default
```

## Manage from the console

The agent schedules jobs via its tools, but operators can also view and manage
them directly from the React console's **Schedule** surface — list current
jobs, open one for the full prompt + details, create, **edit in place**, and
cancel. It's backed by these operator-API endpoints:

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/api/scheduler/jobs` | List jobs (`{jobs, backend}`) |
| `POST` | `/api/scheduler/jobs` | Create — `{prompt, schedule, job_id?, timezone?}` → `{job}` |
| `PUT` | `/api/scheduler/jobs/{id}` | Edit in place — `{prompt, schedule, timezone?}` → `{job}` (id/created_at/last_fire preserved, next_fire recomputed) |
| `DELETE` | `/api/scheduler/jobs/{id}` | Cancel → `{canceled}` |

A malformed `schedule` returns `400` and leaves the job untouched.

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

Every job is namespaced by `AGENT_NAME` so spinning up
`gina-personal` alongside `gina-work` on the same box doesn't
cross-fire prompts: the DB path is per agent
(`/sandbox/scheduler/<agent_name>/jobs.db`, falling back to
`~/.protoagent/scheduler/<agent_name>/jobs.db`), every row also carries
`agent_name`, and all reads/writes filter on it.

If you supply your own `job_id` in `schedule_task`, the id is stored
as-is. Two agents sharing one DB path with the same user-supplied id will
trip a primary-key collision (the second add raises a clear error). To
avoid it, let the scheduler auto-generate (the auto-id is `<agent>-<uuid>`).

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
Before ADR 0003 a fired prompt minted a throwaway context and its answer
was evicted unseen.

### Missed-fire recovery

On startup, jobs whose `next_fire` is in the past are inspected:

- **Within the last 24h** — fire on the next tick (so a 5-minute
  outage doesn't lose an upcoming reminder).
- **Older than 24h** — cron jobs roll forward to the next slot
  without firing; one-shot jobs are dropped. Avoids flooding the agent
  with stale prompts after a long downtime.

### Persistence path

```bash
# Default (Docker)
/sandbox/scheduler/<agent_name>/jobs.db

# Local fallback (when /sandbox isn't writable)
~/.protoagent/scheduler/<agent_name>/jobs.db

# Override
export SCHEDULER_DB_DIR=/var/data/agents
# → /var/data/agents/<agent_name>/jobs.db
```

Mount a volume at the configured path to survive container
restarts (analogous to `audit/` and `knowledge/`).

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
