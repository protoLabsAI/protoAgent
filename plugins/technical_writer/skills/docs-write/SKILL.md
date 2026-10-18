---
name: docs-write
description: Use when writing or revising documentation, a guide, tutorial, API reference, or technical explanation from a codebase.
---

# Write toward an observable result

Start with the evidence brief from `docs-discover`, or establish the missing facts.
Choose the page's job before outlining it:

| Type | Shape | Completion evidence |
| --- | --- | --- |
| Tutorial | A bounded learning outcome, prerequisites, guided steps, checkpoints, cleanup | A learner reaches the stated result |
| Task guide | Goal, prerequisites, shortest complete procedure, result, recovery | A reader completes one real task |
| Reference | Scope/version, exact syntax or fields, defaults, constraints, examples | Every stated contract resolves to source |
| Explanation | Question, model, relationships, tradeoffs, links to actions | The reader can explain why the behavior exists |

1. Reuse the canonical page if it owns this task; otherwise choose a location consistent
   with navigation. State prerequisites including versions, credentials, permissions,
   costs, or destructive effects only where they actually apply.
2. Draft the procedure or explanation. Use exact labels and executable examples from
   the target code. Separate source-install, packaged, container, and hosted paths when
   their commands differ. State a checkable expected result at important transitions.
3. Include the likely failure and recovery path. Link detail that would interrupt the
   main task. Do not hide necessary setup in footnotes or mix migration history into
   first-run steps. Keep examples free of private values; label illustrative output.
4. Edit for one purpose per section, familiar wording, and explicit actors/actions.
   Remove repeated explanations; link to the owner. Keep meaningful caveats and edge
   cases. Prefer a table only for comparable fields and a diagram only when it explains
   relationships better than prose. Use `docs-media` for assets needing production.
5. Check the result from the reader's starting state. Run safe examples where feasible;
   record what was source-verified only. Load `docs-verify` for delivery.

Done when the page has a clear audience and outcome, every behavioral claim is grounded,
examples and links are checked to the stated level, and navigation exposes the page.
