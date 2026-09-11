---
name: consolidate-memory
description: Reflective maintenance pass over long-term memory — merge duplicates, retire stale or completed items, sharpen what stays. Use when the operator asks to clean up/organize memory, when recall keeps surfacing outdated facts, or periodically as a scheduled task.
---

# Memory consolidation

Goal: a future session orients fast — who the operator works with, what
they're focused on, how they like things done — without re-asking. Tools:
`memory_list`, `memory_recall`, `memory_stats`, `forget_memory`,
`memory_ingest`.

## 1. Take stock

`memory_stats` for the shape, then `memory_list` per domain. Note overlaps,
staleness candidates, and thin entries. Spot-check with `memory_recall` on
the operator's recurring topics — what surfaces first is what future
sessions will see.

## 2. Consolidate

- **Durable vs dated.** Preferences, working style, key people, recurring
  workflows are durable — keep and sharpen. Projects, deadlines, one-off
  tasks are dated — once passed or done, retire them (`forget_memory` with a
  reason), first folding any lasting takeaway into a durable entry.
- **Merge overlaps.** Same person/project/preference in several entries →
  one rewritten entry (`memory_ingest` the merged version, then forget the
  fragments).
- **Absolute time.** Rewrite "next week" / "this quarter" / "by Friday" to
  real dates — relative time rots.
- **Drop the re-findable.** Anything recoverable on demand from connected
  tools or files doesn't need remembering. Keep what's hard to re-derive:
  stated preferences, the why behind decisions, who to ask for what.

## 3. Report

Close with a short summary: entries merged, retired, rewritten — and one
line on what future sessions will now get right that they previously
wouldn't. If this pass keeps being useful, offer to make it a monthly
scheduled task (schedule skill).
