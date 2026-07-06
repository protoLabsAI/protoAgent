---
name: due-diligence
description: >-
  /due-diligence — validate a technology or architecture choice with the
  due-diligence workflow: codebase map + external research in parallel,
  antagonist + claim verification, and a cited adopt/build/defer verdict.
user_only: true
slash: due-diligence
---

# Due Diligence — evidence before commitment

Drive the `due-diligence` workflow (plan M2): a codebase-mapper reads what our
code already does about the question while a researcher gathers the external
evidence; an antagonist steelmans the opposing case and a verifier checks the
load-bearing claims; the synthesis is a verdict document that ends in
**adopt / build / defer** with conditions. You scope the question, run it, and
present the verdict — you do not research inline.

## 1. Scope the question

A DD run on a vague question wastes six subagent stages. Before running,
sharpen it to the shape "should we <adopt X | build Y | change Z> for
<purpose>?" —

- If the user named a technology but not the job, ask for the job ("evaluate
  Redis" → for what — cache, queue, session store?).
- If alternatives matter, name them in the question ("…vs keeping SQLite").
- Capture scope hints for the mapper in `context`: which project (registry
  name), which subsystems/dirs the decision touches, any ADR the user knows
  is relevant.

One clarifying exchange at most — then run with the best question you have.

## 2. Run the workflow

```
run_workflow("due-diligence", {"question": "<sharpened question>", "context": "<scope hints or omit>"})
```

Five subagent stages (two parallel pairs + synthesis) — tell the user it's
running and that it reads their registered code as part of the evidence.

## 3. Present the verdict

Lead with the verdict block (**adopt / build / defer** + rationale +
conditions + revisit trigger) — it is the deliverable; the evidence sections
back it. Keep the antagonist's strongest surviving counterpoint visible even
when the verdict goes the other way. If the verdict is `defer`, make the
conditions actionable (what to prove, how). Offer the natural follow-ups:
save the document to notes/memory, or — when the verdict implies a decision
worth recording — draft the ADR (the `adr-authoring` skill has the house
conventions).
