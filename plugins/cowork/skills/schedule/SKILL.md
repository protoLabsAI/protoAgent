---
name: schedule
description: Turn work into a scheduled task that runs unattended — "every morning", "each Friday", "remind me in an hour", "run this weekly" — or adjust an existing schedule. Use whenever the operator wants something recurring, delayed, or re-timed.
---

# Scheduling recurring work

protoAgent's scheduler runs full agent turns on a cadence — any cron
expression, or a one-shot at an ISO timestamp. The tools: `schedule_task`,
`list_schedules`, `cancel_schedule`.

## Changing an existing task

Rescheduling, rewording, pausing: find it with `list_schedules`, then cancel
and re-create with the revised prompt/cadence. Confirm which task before
touching anything if more than one could match.

## Creating a new task — distill the session

You are compressing this conversation into a prompt that will run **cold**,
with no access to anything said here. The discipline:

1. **Identify the repeatable core.** Not "what did we do today" but "what
   should happen every time".
2. **Write a self-contained prompt.** Second-person imperative ("Check …",
   "Compile …", "Save the report to …"). Include every file path, URL,
   account, tool name, and output convention it needs; convert every
   relative reference ("this folder", "like last time") into an absolute
   one. Include what *done* looks like and any preferences the operator
   stated.
3. **Name it** short and kebab-case: `daily-inbox-digest`,
   `friday-metrics-rollup`.
4. **Pick the cadence.** Cron for recurring (`0 8 * * 1-5` = weekday
   mornings); an ISO datetime for one-shots ("remind me in an hour" =
   now + 1h). If the operator didn't give a clear cadence, propose one and
   confirm before creating.

Then call `schedule_task`. Read the created task back to the operator —
name, cadence in plain words, and one-line summary of what it will do —
so a mis-distilled prompt gets caught now, not at 6am.

## protoAgent advantages worth using

Scheduled tasks here run on *this* machine: they can touch local folders,
use any cron cadence, and their results land in the activity thread — and
are indexed into memory, so a later session can recall what any run
produced. Big recurring deliverables can also pair with a workflow recipe
— mention it when a task outgrows a single prompt.
