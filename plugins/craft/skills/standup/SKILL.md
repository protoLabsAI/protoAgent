---
name: standup
description: >-
  /standup — an operational status report on everything this agent currently
  owns: tasks, goals, schedules, background work, and what needs the
  operator's call.
user_only: true
slash: standup
---

# Standup

Produce a status report on the work this agent owns — formatted for a fast
scan, honest about gaps, ending in decisions rather than chatter.

## 1. Gather

Collect from every surface this instance actually has, in parallel where the
reads are independent (`task_batch` for anything that needs digging). Skip a
surface silently only if its tools aren't installed; if a read fails, say so
in the report rather than omitting it:

- **Tasks** — the tasks board: what's open, in progress, blocked.
- **Goals & drives** — active goals, their verifier state, recent progress.
- **Schedule** — scheduler jobs: what fires next, anything failing.
- **Background work** — recent background reports and running workers.
- **Session work** — what this conversation has produced or promised.
- **Code/PRs** — only if git/GitHub tools are available: open PRs, CI state.

## 2. Report

```
## Owned now
- 🟡 <in-flight item> (state — what it's waiting on)
- ⬜ <queued item>

## Done since last standup
- ✅ <item> (evidence: link/id)

## Gated / blocked
- 🚨 <item> — <what unblocks it, and whose move it is>

## Schedule
- <job> — next fire <when> (or "no scheduled jobs")

## Needs your call
- <decision> — <your recommendation>
```

Conventions: ✅ done · 🟡 in flight · ⬜ queued · ⬛ deferred (reason inline) ·
🚨 blocked/escalation (sparingly). Don't oversell — half-done is more honest
than an inflated status. Name blockers by id, not vibes.

The report is ephemeral: deliver it in chat, don't persist it anywhere.
