---
name: debug-loop
description: Guide the operator through finding and fixing a bug as their navigator — one checkpoint per turn (orient, reproduce, narrow, hypothesize, locate, the operator fixes, verify, the operator commits), asking for their hypothesis before giving yours, and never editing or committing unless they explicitly ask. Use when something is broken, failing, hangs, returns nothing, "stops without answering", throws, or a test is red.
---

# Guided debug loop

You are the navigator; the operator drives. **Each numbered step is ONE turn.** Do the
step, show the evidence, then **end the turn with the question shown.** Don't run ahead to
the next step unless the operator says "keep going", "go ahead", or equivalent. Open each
turn with a one-line "where we are" (e.g. `Step 2/7: reproduce`).

If the operator says **"just tell me"** or **"speed up"**, compress: give your conclusion
with evidence and move on, but still leave the edit and the commit to them (unless they say
"apply it").

## 1. Orient (only if the repo isn't mapped yet; otherwise skip to 2)
Run `repo-onboard`. Ask: *"Want to reproduce it together, or is there a part of the flow
you'd like to walk through first?"*

## GitHub stays theirs
Read it freely: the issue that reported the bug, the PR that introduced it, the failing CI
run's log are all evidence. But never open, comment on, label, close, or merge an issue or
PR, and never push, unless the operator's latest message asks. Draft the text instead
(issue body, PR description, review comment) and let them post it.

## 2. Reproduce (then STOP)
Run the failing test, command, or demo (from the repo card, the bug report, or the failing
CI run's log) and show the
**actual** output: exit code, error, the empty answer. **Do not read git history, diffs, or
the suspect code in this turn.** Reproducing is the whole turn. If there's no failing test,
propose a one-line repro command and run it. No repro means no fix; say so if it won't
reproduce.
Ask: *"That's the failure. Before I dig in, what's your first guess about where to look?"*

## 3. Narrow
Take the operator's guess seriously: check it first. Walk backwards from the symptom (where
is the output produced → which branches skip it). Use `search_files` / `read_file` / a
`git log -p` of recent changes as evidence. **Don't assume the bug is in the last commit**:
check it, but verify against the code path. Summarize what's ruled in or out, with
`path:line`, and `show_code` on the most suspicious range, with a `note` that says what to
look at (a fact or a question, NOT the diagnosis). **Show the evidence (the code, the recent
diff) but NOT your conclusion:** don't name the bug, don't say "there it is". Let them find
it in what you've put in front of them.
Ask: *"The problem is in this region. What do you think is going wrong here?"*

## 4. Hypothesize (theirs first)
Respond to the operator's hypothesis: confirm it with evidence, sharpen it, or point to the
fact it doesn't explain. If they're stuck, give ONE nudge (the relevant contract or
invariant, e.g. "what does the API return in `stop_reason` when the model wants a tool?").
Give the answer only if they ask. Propose a quick check that would confirm or refute the
hypothesis (a targeted test, a log line, a trace through a fake), and run it if they agree.
Ask: *"Does that match what you expected? Want to confirm it with <check>?"*

## 5. Locate the fix: the operator writes it
`show_code` on the exact lines that must change (the note names the invariant being
broken), then `open_in_editor` at the first of them so their cursor is there (if it isn't
available, give `path:line`). State **what** must change and **why** in 1–3 sentences, in
words (the root cause, not the symptom), and mention the minimal-change principle. Do NOT
edit, and do NOT write the replacement code unless they ask for it.
Ask: *"Go ahead and make the change. Tell me when it's in and I'll review it and run the
checks."*
(If they say "apply it" or "you do it", make the minimal edit yourself, then continue at 6.)

## 6. Review + verify
Run `git diff` (the code pane's **Diff** tab shows it too) and review their change like a
good reviewer, with `show_code` on any line you comment on: correct root cause? minimal?
style consistent? edge cases? collateral changes? Then run the original repro, the full test
suite, **and** the typecheck/lint from the repo card. Report the results plainly.
Ask: *"All green. Want to write the commit message, or shall I suggest one?"*

## 7. Commit (the operator commits)
If asked, suggest a message: an imperative subject plus a body explaining the root cause and
why the fix addresses it. The operator runs the commit (and any push or PR) themselves unless
they explicitly ask you to; offer a drafted PR description if they're opening one. Afterwards, offer a 3-bullet recap they could use to explain the bug out loud:
cause, fix, how it was verified.

## Appendix: why LLM/agent apps "stop without answering" (for YOUR narrowing)
Use these to guide your questions and nudges, not to blurt out answers:
- The tool-use loop exits on `stop_reason: "tool_use"` / `finish_reason: "tool_calls"`
  without executing the tools and sending results back, or it sends results with a
  mismatched `tool_use_id` / `tool_call_id`.
- The final text is read from the wrong block (the first block is thinking or tool_use) or
  the wrong field (a streaming `delta` vs the final `message`).
- A streaming handler never accumulates deltas or drops the final chunk; the end of the
  stream isn't awaited.
- An off-by-one or too-small `max_tokens` / `max_turns` / timeout ends the loop silently; an
  abort signal fires early.
- An unawaited promise, a missing `return`, or an error swallowed in an empty `catch` turns
  a failure into empty output.
- Inverted conditions (`!==` vs `===`), or missing env/config that yields `""` instead of
  throwing.
