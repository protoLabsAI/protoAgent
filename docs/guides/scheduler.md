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
<figure class="docs-media"><img src="/media/schedule-state/v1.png" alt="The sample job shows a daily schedule in America/Los_Angeles, next and last fire times, its prompt, and Edit and Delete controls." loading="lazy" style="max-width:100%;height:auto"><figcaption>Inspect the saved timezone and next fire. Last fire records dispatch; confirm the completed reply in Feed.</figcaption></figure>
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

## What the agent sees each turn

The agent doesn't have to call `list_schedules()` to remember what it queued. Every
turn, the `<working_state>` block shows its pending schedules, sorted by chat:

- **This chat.** Each `wait` resume or schedule that fires into this chat, or that
  this chat created, is shown with its next fire time, a note that it runs
  automatically unless the agent cancels it with `cancel_schedule("<id>")`, and its
  full instruction (up to 1,500 characters, with a marker if it's cut). At most five
  are shown this way.
- **Agent-wide.** Jobs not tied to any chat, such as an Activity-thread cron, and
  recurring schedules made in other chats (they fire into Activity whichever chat is
  open). One short line each.
- **Other chats.** One-shot jobs that belong to another chat appear only as a count.

The job whose fire started the current turn is marked `[FIRING NOW]`, so the agent
doesn't take it for another queued wake. Active watches follow the same rules: a
watch that reacts in this chat shows its full reaction prompt and a reminder that
`clear_watch("<id>")` stops it. See
[ADR 0127](/adr/0127-compaction-fidelity) D5.

## Standing directives {#standing-directives}

When you approve or instruct something that outlasts the current turn, the agent records
it in the chat's directive ledger with `record_directive`, quoting your words. For example:

> Once #4801 and #4808 have both merged, cut the release. If the review gives the release PR
> a WARN or FAIL, come back to me instead of merging.

Every turn after that, `<working_state>` shows it under **STANDING DIRECTIVES (from the
operator — in force until fulfilled or revoked)**, verbatim, with any conditions. The
ledger is stored outside the transcript, so a compaction can't drop the approval, and the
agent checks the ledger before it decides it lacks approval. When the work is done the
agent closes the directive with `fulfil_directive`.

To see or withdraw what the agent holds:

- Run `/directives` in the chat. It lists the directives in force and the closed ones, with
  who closed each and why.
- Run `/directives revoke <id> [reason]` to withdraw one. The agent stops seeing it on its
  next model call.
- Call `GET /api/chat/sessions/{id}/directives` and
  `DELETE /api/chat/sessions/{id}/directives/{directive_id}` from a script. See
  [Operator API](/reference/operator-api#chat-sessions).

Directives belong to one chat; other chats don't see them. Deleting or clearing the chat
removes its directives. There is no console panel yet. See
[ADR 0127](/adr/0127-compaction-fidelity) D6 and the
[directive tools](/reference/starter-tools#record_directive).

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
