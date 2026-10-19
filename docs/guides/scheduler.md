# Schedule future work

For a first example with a completion check and cleanup, follow
[Run your first recurring task](/tutorials/first-schedule).

Ask the agent to run a task later or on a recurring schedule:

> Every Monday at 9am in America/Los_Angeles for the next month, summarize the
> previous week's pipeline incidents.

Open **Schedule** to confirm the saved prompt, timezone, and next fire time.
The server must be running to execute the job. Recurring runs appear in
**Feed**; chat-created schedules also report back to their originating chat.

## Manage from the console

Open **Work → Schedule** to create, inspect, edit, or cancel a job. Use a named timezone
for recurring local times; the new-schedule dialog starts with your browser’s local timezone; API calls
without a timezone use UTC. One-shot ISO timestamps use their
own offset, so include one when specifying local time.

Write a self-contained prompt with the sources, action, and expected output.
Recurring runs do not inherit the conversation that created them. An agent-tool
one-shot resumes its originating session; jobs created outside chat normally
appear in **Feed**.

<!-- docs-media: schedule-state -->

<!-- /docs-media -->

## Scheduling tools

| Tool | Purpose |
| --- | --- |
| [`schedule_task`](/reference/starter-tools#schedule_task) | Save a five-field cron schedule or one-shot ISO-8601 datetime |
| `list_schedules()` | Inspect this agent's jobs |
| `cancel_schedule(job_id)` | Remove a job |

Recurring schedules created by the **agent tool** get a default `ttl` of `7d`.
Request a longer lifetime for ongoing work. `ttl` accepts values such as `24h`,
`2w`, or `P7D`; `max_fires` limits successful firings. The seven-day default still
applies when only `max_fires` is supplied. These expiry settings apply to cron
jobs, not one-shots. The local backend checks expiry after a successful fire,
not at the exact TTL timestamp. They are not fields on the console's scheduler REST API,
and its jobs do not acquire the tool's default TTL.

## Enabling / disabling

Scheduling is on by default. Set `middleware.scheduler: false` to disable it,
or use `SCHEDULER_DISABLED=1` for a process-level override. The tools are
unavailable while disabled. See [scheduler configuration](/reference/configuration#scheduler).

## Multi-agent isolation

Schedules live in `<instance_root>/scheduler/agent/jobs.db`. Renaming an agent
keeps that store; an older name-keyed store may be adopted on first access.

`SCHEDULER_DB_DIR` overrides the parent directory: jobs then live at
`<override>/<agent_name>/jobs.db`. Only one process can own the scheduler's
polling lock. Give independent agents separate roots; see
[Run multiple instances](/guides/multi-instance).

## How firing works

The local backend polls for due jobs once a second and delivers prompts through
the server's A2A endpoint. It does not claim a job again while that job is firing.
Cron jobs advance to their next slot when claimed; one-shots are removed after
successful execution. Failed one-shots remain for retry. Repeated turn failures
are recorded and backed off rather than disabling the job permanently.

### Missed-fire recovery

On startup:

- Jobs overdue by at most 24 hours fire on the next tick.
- Older cron jobs advance to their next slot without firing; older one-shots
  are dropped.

Check schedules before reopening a [restored backup](/guides/backup-and-restore):
a recent missed job can run immediately.

### Persistence path

Run `protoagent config explain` to find the active paths. Defaults are
`~/.protoagent/default/scheduler/agent/jobs.db` on a normal host and
`/sandbox/scheduler/agent/jobs.db` in the bundled Docker image. Preserve the
instance volume across container restarts.

## Adding a case to your eval suite

Schedule a near-future one-shot, wait for its fire, and assert the fired prompt's
side effects in the audit log or store. A normal single-turn evaluation does
not establish that the later task executed. See [Evals](/guides/evals).

## References

- [Watch an external condition](/guides/watches)
- [Goal mode](/guides/goal-mode)
- [Reusable workflows](/guides/workflows)

<span id="operator-api"></span>
<span id="plugin-owned-recurring-jobs"></span>

## Related reference

See [Automation integration](/reference/automation) for scheduler endpoints and plugin-owned jobs.
