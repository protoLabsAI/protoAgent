---
name: recording-friction
description: >-
  When and how to record friction with `record_friction` — the moment a tool is
  missing, awkward, or wrong, and the moment you catch yourself taking a long way
  round. Read this when you hit a rough edge, when you reach for a shell to do
  something that should be a tool, or when you are triaging the friction backlog.
---

# Recording friction

The friction ledger is how this system improves itself. Auto-capture already logs
tool errors and escape-hatch reaches; it cannot see the thing only you know — that
a tool was *there* and *awkward*, that an error message sent you the wrong way, or
that you got the right answer by the wrong route.

**The bar is low on purpose.** An unrecorded friction point costs the next agent
the same hour it cost you. A slightly noisy ledger costs one line.

## Record it at the moment it happens

Not at the end of the turn, not "if it comes up again". You will have moved on and
the specifics — the exact path, the exact error, the call you wished existed — are
the whole value. A summary written from memory an hour later is a complaint; one
written in the moment is a spec.

## Which channel

`kind="harness"` — **the tooling should change.**

- A tool you needed did not exist.
- A tool existed but made you do it in N calls when one would have done.
- An error message was wrong, vague, or pointed at the wrong cause.
- You reached for a shell/`run_command` for something that should be first-class.
- A limit bit you (truncation, pagination, a cap) and you had to work around it.

`kind="model"` — **you should have known better.** A wrong path you recognized,
a weak answer, a retry you caused. This is a labeled trace, not self-flagellation;
record it and move on.

## Severity

`major` when it **blocked** you or produced a wrong result you had to undo.
`minor` when it cost you time or elegance. Be honest in both directions — everything
marked major is the same as nothing marked major.

## Write it so it can be fixed

A summary is one line, and it is a **claim about the system**, not a feeling:

- ✅ `read_file truncates at 50k chars — reconstructing a 167KB file took ~8 search_files calls`
- ❌ `read_file was annoying`

Then put the reproduction in `detail`: what you were doing, what happened, and —
the part people forget — **what would have helped**. "A `from_line`/`to_line`
parameter would have made this one call" is the sentence that turns a report into
a ticket.

Keep the summary under ~200 characters and the detail under ~600: the ledger caps
both on write, and it cuts mid-word.

## Do not record

- A tool refusing you on purpose (a permission gate, an approval pause, a HITL
  interrupt). That is the system working.
- A failure you caused and immediately fixed with no lesson in it.
- The same friction twice in one turn — identical summaries are grouped, so say it
  once and let the count speak.

## Reviewing and closing the loop

`friction_review` reads the backlog; open friction also appears in your
`<working_state>` under **OPEN FRICTION**, so you do not need to poll for it.

Call `resolve_friction` **when the rough edge is actually fixed** — the fix merged,
the tool shipped. It stamps `resolved_at` in place (nothing is deleted, the audit
trail survives) and drops the entry from the backlog and from the operator's console
alike. Do not use it to quiet a signal you have not fixed; a live problem marked
resolved is worse than one never recorded, because now nobody is looking.

If a friction point warrants a tracked fix, file it in the repo that owns the fix
and say so in the resolve reason.
