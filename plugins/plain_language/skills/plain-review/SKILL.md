---
name: plain-review
description: >-
  /plain-review — check a text (pasted, a file, or your last reply) against the
  plain-language guide and report findings by severity, with a suggested fix for
  each. Review only: it doesn't rewrite unless asked.
user_only: true
slash: plain-review
---

# /plain-review: is this clear to its reader?

Review the text the operator points at: text after the command, a file they
name, or, if they give nothing, your own previous reply. This is a review.
Don't change the text unless they ask. If they then want it fixed, use
`plain-language-edit`.

1. **Name the reader.** Say who you think the text is for, in one line. If you
   can't tell, ask once. Every judgment below depends on the answer.
2. **Run the mechanical check.** Call `plain_language_check` on the text. Read
   each passive-voice and hidden-verb hit in context and drop the false ones.
3. **Review what the tool can't see.** Read it as that reader and check:
   - Is the bottom line or the action in the first two sentences?
   - Is the order the reader's order (steps as they happen, common cases before
     exceptions)?
   - Does each paragraph cover one topic? Are audiences mixed?
   - Is there jargon or an undefined term this reader wouldn't know? Is one
     thing called by two names?
   - Do the headings say what each section answers? Do the lists have lead-ins
     and parallel items?
   - Is anything missing that the reader needs in order to act?
4. **Report.** Start with a two-line verdict: who it's for, and whether that
   reader will get it the first time. Then give the findings, most serious first,
   at most 12:

   | Severity | Where | Problem | Suggested fix |
   |---|---|---|---|

   - **Blocker**: the reader could misunderstand or take the wrong action.
   - **Major**: it slows the reader down a lot (bottom line buried, jargon,
     wrong order).
   - **Minor**: wordiness and style (filler, a complex word, passive voice).

   Group repeated minor findings into one row ("filler words, 6 places") instead
   of listing each one. End with the check tool's counts on one line.

Don't pad the report. If the text is already clear, say so in two lines.
