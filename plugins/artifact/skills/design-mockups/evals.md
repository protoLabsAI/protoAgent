# Design-mockup evals

A small, hand-labelled prompt set for the `design-mockups` skill: each row is a request paired
with **whether this skill should pick it up**, and if so, what a good run has to contain. It is
the ground truth for a later routing experiment and a reviewer's sanity check of the workflow in
`SKILL.md`.

The two routes: **mockup** (this skill: real data, toggle bar, every state, check at both
widths, then `open_artifact`) and **artifact** (the `rendering-artifacts` skill: a chart,
diagram or widget, no review workflow).

| # | Prompt | Expected route | Why |
|---|---|---|---|
| 1 | Mock up the Decisions panel so I can review it before we build it. | mockup | a screen to review before building |
| 2 | What would the knowledge collections view look like with real collections in it? | mockup | "what would this screen look like", with real data |
| 3 | Make a preview of the directive ledger card, phone and desktop, light and dark. | mockup | widths and themes are the review checklist |
| 4 | Show me every state of the inbox empty/loading/error screens. | mockup | covering states is the point |
| 5 | Give me a link I can open in my browser to review the new settings page design. | mockup | a reviewable design plus the browser link |
| 6 | Redo the mockup from earlier with the shorter copy. | mockup | iterate: `update_artifact` on the same id, re-check, new link |
| 7 | Chart signups by month. | artifact | a chart is an answer, not a design under review |
| 8 | Draw the login retry flow as a diagram. | artifact | a mermaid diagram |
| 9 | Build me a tip calculator. | artifact | a small working widget, not a design |
| 10 | Write the design doc for the billing flow. | artifact | a document → `show_artifact(kind="markdown")` |

What a good mockup run contains (rows 1–6):

- Data read from the real source first (a tool or API call, a file read, or the user's
  screenshot) before `show_artifact`, and that data in the mockup: no placeholder names.
- `show_artifact(kind="html", placement="panel")` with only `pl-*` classes and `--pl-*`
  tokens for colour and type; layout-only custom CSS.
- The toggle bar: System / Light / Dark, Desktop 1440 / Phone 390, and a state picker covering
  empty, one, many, loading, failed and at least one edge case from the data.
- `check_artifact` after the show; on a failure, one targeted `update_artifact` and one more
  `check_artifact`.
- `open_artifact` last, with its link in the reply, plus a one- or two-sentence summary.
- Row 6: the SAME artifact id, a new version, and a fresh `open_artifact` link.
