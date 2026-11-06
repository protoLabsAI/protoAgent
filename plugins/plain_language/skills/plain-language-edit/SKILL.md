---
name: plain-language-edit
description: >-
  Use when asked to rewrite, simplify, tighten, clarify, or "make readable" an
  existing text (a doc, email, policy, message, or pasted paragraph), or to
  translate jargon into plain English. Rewrites it in plain language, keeps every
  fact, and reports what changed.
---

# Rewrite in plain language

The goal is the same meaning, clearer. A rewrite that loses a fact, a caveat, a
number or a step is a worse rewrite, however short it is.

1. **Find the reader and the job.** Work out who reads this and what they must
   know or do afterward. If the text is for several audiences, say so. Ask only
   when you can't tell and the answer would change the rewrite.
2. **List what must survive.** Before you change anything, note every fact,
   number, date, requirement, deadline, condition and warning in the original.
3. **Check a long original.** If it's about 300 words or more, run
   `plain_language_check` on it to find the mechanical problems quickly. For a
   shorter text, read it yourself and skip the tool.
4. **Restructure first, then edit sentences.** Move the bottom line or the action
   to the top. Order steps the way the reader does them. Split paragraphs that
   cover more than one topic. Only then fix sentences: active voice, verbs instead
   of nominalizations, common words, no filler, one term per idea. Load
   `plain-language-write` for the full checklist.
5. **Keep the right technical terms.** Replace jargon the reader doesn't use.
   Keep terms the reader needs (a command name, a legal term, a product name),
   and define them at first use.
6. **Check the rewrite.** If you ran the check in step 3, run it again and fix
   what's left. Then go through your list from step 2 and confirm each item
   survived.

## What to hand back

Give the rewritten text, ready to use, in one version. People often paste it
straight to its readers, so keep notes out of it.

- Add notes after the text only when the operator must act on them. For
  example, the original contradicts itself, or a fact looks wrong. Keep each to
  one line.
- Don't flag something as unclear when the original settles it. A doubt you add
  becomes the reader's doubt.
- List what you changed, and the check counts, only if the operator asks.
