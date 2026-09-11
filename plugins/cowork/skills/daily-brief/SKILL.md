---
name: daily-brief
description: Compose the operator's daily brief — schedule, things waiting on them, ongoing work, and heads-ups. LOAD THIS SKILL FIRST (load_skill) and deliver the brief as an HTML artifact via show_artifact — never as chat text. Run only when the operator explicitly asks for their brief (or /daily-brief), or when executing a scheduled brief task. An ordinary question about today's calendar is answered directly, not with a brief.
user_facing: true
slash: daily-brief
---

# Daily brief

One page that answers three questions before the first coffee is finished:
*what's my day shaped like, who is blocked on me, and what was I in the middle
of?* Tell the operator it takes a couple of minutes, then work quietly and
deliver the page.

## Take stock of sources

Use what's connected; skip what isn't — the brief adapts rather than
apologizing. In order of value:

1. **Memory** — `memory_recall` for the operator's active projects, stated
   priorities, and preferences. This is what makes the brief *theirs*: an
   event titled "sync" means nothing until memory says which project it
   belongs to.
2. **Calendar** — today's events; glance at tomorrow morning only to decide
   whether today needs prep time for it.
3. **Email** — conversations where the operator owes a reply: someone asked
   *them* specifically and the thread has gone quiet on their side. A thread
   where anyone on a list could answer doesn't count. If nothing qualifies,
   fall back to what's unread and recent, clearly labeled as such.
4. **Overnight work** — anything that ran unattended since yesterday:
   `list_schedules` for what fired, and scheduled/background results are
   indexed into memory, so `memory_recall` surfaces what a run produced.
   Something finished while the operator slept belongs on the page; a run
   that failed belongs higher.
5. **Work folders** — files modified in the last day or two in the fenced
   folders: yesterday's deliverables are today's follow-ups.
6. **Notes / tasks** — anything explicitly marked for today.

Budget the calls: one pass per source, no fishing expeditions. A brief that
takes ten minutes to gather has already failed its job.

## Decide what makes the page

Everything competes for a small page. Keep an item only if the operator would
act on it *today*: a meeting they must prepare for, a person waiting on them,
work mid-flight with a deadline in sight. Fold the rest into one quiet line or
drop it. Never pad — a light day should *look* like a light day; that is
information too.

Order: the day's shape first (schedule with prep flags), then **waiting on
you** (people first, oldest ask first), then **in flight** (ongoing work with
its next action), then a short **heads-up** (tomorrow's early meeting, an
approaching deadline, a stale promise memory knows about).

## Render

The brief IS the artifact — always deliver it with `show_artifact(kind="html",
...)`, never as chat markdown, even on a light day (a light day gets a light
page; the chat reply is one pointer line, "Your brief is in the panel").
Self-contained: system font stack, no external assets or network fetches,
readable in light and dark.
Design it like a well-set newspaper front page, not a dashboard — a dated
masthead, one strong opening line summarizing the day in plain words, clear
sections, generous whitespace. Timestamps in the operator's timezone; every
item phrased as a decision or action, never raw data dumps.

If a writing-voice profile exists (`my-writing-style`), the prose follows it.

## Recurring setup

If the operator asks for this every morning (or any cadence): use the schedule
skill's discipline — distill into a self-contained prompt that names the
sources to check, the fenced folders by path, the delivery form (HTML artifact
via show_artifact), and the language to write in (the language the operator
used when asking). protoAgent cron is unrestricted: "weekdays at 7" is
`0 7 * * 1-5`, but Sunday-evening week-ahead briefs or first-of-the-month
reviews work just as well.

## Ground rules

- Explicit invocation only — never volunteer a brief mid-conversation.
- Read-only gathering: composing the brief must not send, reply, archive, or
  modify anything.
- If every source is disconnected, say so and point at Settings ▸ Plugins —
  don't render an empty page.
