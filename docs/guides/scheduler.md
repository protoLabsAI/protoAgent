# Schedule future work

protoAgent ships a scheduler so the agent can defer tasks to itself —
"remind me about X tomorrow", "every Monday morning summarize last
week's logs", "at 3pm check the deploy". Two backends ship by default;
the agent-facing tool surface is identical regardless of which one is
active.

## When to read this

- You want forks (or your own multiple agents) to support reminders,
  recurring sweeps, or any "do this later" intent.
- You're running protoWorkstacean and want scheduled fires to flow
  through the existing bus.
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

## Backend selection

`server.py::_build_scheduler` picks at startup:

1. `middleware.scheduler: false` in YAML → no scheduler. The three
   tools don't ship. (Symmetric with `middleware.knowledge` /
   `middleware.memory` — drawer/wizard editable.)
2. `SCHEDULER_DISABLED=1` env → no scheduler. Runtime escape hatch
   for fleet operators who can't edit config.
3. `WORKSTACEAN_API_BASE` + `WORKSTACEAN_API_KEY` set →
   **`WorkstaceanScheduler`**.
4. Otherwise → **`LocalScheduler`** (sqlite, asyncio polling).

Both backends honor the same `SchedulerBackend` protocol; the agent
loop never knows which one is wired up. The scheduler is **default
on** — explicitly opt out via either config path above when a fork
wants a stateless agent with no scheduling surface.

```bash
# Solo / local dev — falls through to LocalScheduler automatically.
python server.py

# Workstacean install — set both env vars and restart.
export WORKSTACEAN_API_BASE=http://your-workstacean-host:3000
export WORKSTACEAN_API_KEY=<key>
python server.py
```

> **protoLabs operators**: the fleet's Workstacean lives on the
> `ava` node; `WORKSTACEAN_API_KEY` is in the org's secrets manager
> under `secret-management → workstacean`. Coordinate with the team
> for the exact URL.

## Multi-agent isolation

Every job is namespaced by `AGENT_NAME` so spinning up
`gina-personal` alongside `gina-work` on the same box doesn't
cross-fire prompts.

| Backend | How it isolates |
|---|---|
| Local | DB path per agent: `/sandbox/scheduler/<agent_name>/jobs.db` (falls back to `~/.protoagent/scheduler/<agent_name>/jobs.db`). Every row also carries `agent_name`; reads filter on it. |
| Workstacean | Job IDs are prefixed `<agent_name>-...`; topics are namespaced `cron.<agent_name>.<job_id>`. One Workstacean install can serve N forks safely. |

If you supply your own `job_id` in `schedule_task`:

- Local: the id is stored as-is. Two agents sharing one DB path with
  the same user-supplied id will trip a primary-key collision (the
  second add raises a clear error). To avoid it, let the scheduler
  auto-generate (the auto-id is `<agent>-<uuid>`).
- Workstacean: the adapter prepends `<agent>-` if your id doesn't
  already start with it, so cross-agent collisions are impossible.

## Local backend — how firing works

The local scheduler runs an asyncio polling task on FastAPI's
`startup` event. Once a second:

1. Read jobs where `next_fire <= now()` and `enabled = 1`.
2. For each due job: POST to `http://127.0.0.1:<active_port>/a2a` as
   a `message/send` with the job's prompt as the message text. Bearer
   + X-API-Key are forwarded automatically.
3. One-shot ISO jobs are deleted after firing. Cron jobs reschedule
   forward via `croniter`.

Going through HTTP rather than calling into the graph directly buys
parity with real callers — the audit log, cost-v1 capture, and
push-notification path all behave identically.

### Missed-fire recovery

On startup, jobs whose `next_fire` is in the past are inspected:

- **Within the last 24h** — fire on the next tick (so a 5-minute
  outage doesn't lose an upcoming reminder).
- **Older than 24h** — cron jobs roll forward to the next slot
  without firing; one-shot jobs are dropped. This matches
  Workstacean's recovery behaviour and avoids flooding the agent
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

## Workstacean backend — how firing works

When `WORKSTACEAN_API_BASE` and `WORKSTACEAN_API_KEY` are set, the
adapter publishes to `POST {base}/publish` with topic
`command.schedule` and the action wrapper Workstacean expects. See
the [Workstacean scheduler reference](https://protolabsai.github.io/protoWorkstacean/reference/scheduler/)
for the payload shape.

When the schedule fires, Workstacean publishes the inner payload to
`cron.<agent_name>.<job_id>`. **Workstacean does not natively dispatch
to A2A endpoints today** — your fork needs to wire a bridge that
subscribes to `cron.<agent_name>.*` and POSTs to the protoAgent's
`/a2a` endpoint.

### Topic prefix override

If your existing Workstacean bus uses a different convention:

```bash
export WORKSTACEAN_TOPIC_PREFIX="myorg.cron.gina"
# → topics fire on myorg.cron.gina.<job_id>
```

### `list_schedules()` returns empty under Workstacean

Workstacean's `list` action publishes its response on the
`schedule.list` topic — there's no synchronous reply on `/publish`.
The adapter intentionally doesn't subscribe. If you need live
introspection, query Workstacean directly or run the local backend.

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

- [Workstacean scheduler reference](https://protolabsai.github.io/protoWorkstacean/reference/scheduler/)
- [Configuration](/reference/configuration#scheduler) — env vars
- [Eval your fork](/guides/evals) — for the testing pattern above
