# Presentation-ladder evals

A small, hand-labelled prompt set for the `rendering-artifacts` skill: each row is a request
paired with the **tier the skill should pick** (see the ladder in `SKILL.md`). It exists so a
later routing experiment (ADR 0118 D7, deferred) has a ground truth to score against, and so a
reviewer can sanity-check the ladder's guidance by reading realistic prompts.

The four tiers, lowest to highest: **text** → **component** (`show_component`) →
**inline** (`show_artifact(…, placement="inline")`) → **panel** (`show_artifact(…)`).

The rule the labels follow: **prefer the lowest tier that answers**, and **inline for an
answer, panel for a work product**.

| # | Prompt | Expected tier | Why |
|---|---|---|---|
| 1 | What's the capital of Peru? | text | a single fact — just words |
| 2 | Explain the difference between a thread and a process. | text | prose explanation, nothing to render |
| 3 | Write a Python function that reverses a linked list. | text | code in a fenced block reads fine inline |
| 4 | List the eight planets with their orbital periods in days. | component | exact tabular values → a `table` component, not a widget |
| 5 | Show the steps to deploy the service, in order. | component | ordered records → a `timeline`/`steps` component |
| 6 | Where in the repo is `create_agent` defined? | component | a code pointer → a `code-ref` component |
| 7 | Split a $180 bill 4 ways with a 20% tip. | inline | a calculator the user tweaks — an answer they interact with |
| 8 | Make a mortgage calculator I can adjust rate and term on. | inline | a small what-if tool, lives beside the prose |
| 9 | Chart these monthly signups: Jan 120, Feb 150, Mar 210. | inline | a chart from a handful of rows → inline `vega-lite` |
| 10 | Show me an interactive explainer of how binary search narrows a range. | inline | an explainer the user drives, in the conversation |
| 11 | Draw a flowchart of the login retry logic. | inline | a mermaid diagram to look at in-line, not a work product |
| 12 | Build a little color-contrast checker widget. | inline | a self-contained tool — an answer, not a document |
| 13 | Draft a full pitch deck for the Q3 launch. | panel | a multi-slide work product carried across turns |
| 14 | Write up the design doc for the new billing flow so I can edit it. | panel | a document the user opens, edits and returns to |
| 15 | Build a multi-page budgeting app with tabs and saved scenarios. | panel | a large app worked on over many turns |
| 16 | Turn the quarterly numbers into a polished one-page report I can download. | panel | a file-shaped deliverable, not an in-line answer |

Notes for whoever scores these:

- Rows 1–3 stay **text** because climbing a rung would cost more and read slower for no gain.
- Rows 4–6 are **component** because the answer is *structured data*, rendered natively and
  safely (no code runs) — reach past it to an inline artifact only when the user needs to
  *interact*.
- Rows 7–12 are **inline**: the answer is a small thing the user pokes at, and it belongs in
  scrollback next to the explanation.
- Rows 13–16 are **panel**: a work product the user keeps editing, not a one-shot answer.
