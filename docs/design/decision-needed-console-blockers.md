# Decision needed console mount: prerequisite gaps

Part of #4725. Implementation is blocked against the approved
[revision 1 design](decision-needed.md). The mount task explicitly requires:
“Use only those composites and @protolabsai/ui; if something is missing, stop
and say so in the PR (it goes to designSystem).”

This handoff records missing contracts in the merged dependencies, rather than
changing the approved design or introducing a second implementation in the shell.
No console behavior has changed.

## Evidence for designSystem

The checkout includes the merged component slice, PR #4947
(`87597768190938ad18163166bada2d18e9bf7059`), and entry-point slice, PR #4950
(`c217e4b46a6c776ef0ccf45d4199de960d6caba9`).

| Missing contract | Evidence in the merged implementation | Required correction |
| --- | --- | --- |
| DS phone action button | `apps/web/src/decisions/DecisionsHeaderButton.tsx` imports only `Count` from primitives, renders a raw `<button>`, and injects its own chrome and focus-ring stylesheet. `entrypoints.test.tsx` explicitly tests that stylesheet. | Use the DS ghost, md, icon `Button` required by revision 1 §2.2, Q23 and Q24; keep only layout CSS in the shell. Update the composite tests with it. |
| Shared lookup of settled decisions | `apps/web/src/decisions/useDecisions.ts` calls `listDecisions("pending")` and replaces `snapshot.decisions` with that response. `answer()` returns the result to its caller but does not retain the returned row. There is no shared by-id lookup or hydration for a historical transcript. | Provide a shared record lookup that can retain and load answered, expired and withdrawn decisions, while keeping pending count and ordering separate. A chat-local copy would violate the task's shared-store requirement. |
| Frozen transcript presentation | `apps/web/src/decisions/DecisionCard.tsx` has no transcript presentation prop. An answered row renders the “Already answered” callout; a successful local answer stays in the component's “Sent” phase. Neither renders `You chose "{option}" · {time}`. | Add a transcript mode to the same card, preserving the panel's sent hold and 409 behavior. Test both a newly answered card and an answered record loaded with a historical chat. |

These are changes to the prerequisite composites, not absent exports that can be
resolved by mounting them differently. In particular, wrapping the existing card
with a second answer renderer would duplicate its state and presentation.

## Design questions

- **Q13:** No mobile e2e spec currently references `mshell-head-spacer`.
  `MobileShell.test.tsx` does assert the spacer in the empty pushed-view header.
  Preserve that empty case and add a pending case that replaces it with the
  composite; do not remove the existing test.
- **Q15:** There is no single sheet owner today. `App.tsx` owns
  `sessionSheetOpen`, while `useDecisions` owns `panelOpen`. The mount must
  coordinate these states for both a header click and a notification click,
  closing the session sheet before the Decisions sheet becomes visible.
  Simply mounting the header composite creates two independent Drawers.
- **Q23:** The approved design requires a DS `Button`; the merged raw-button
  implementation contradicts that resolved decision. Correction belongs in the
  prerequisite composite.
- **Q24:** The approved design delegates the focus ring to that DS `Button`.
  The merged composite instead injects a custom `:focus-visible` rule. No second
  shell-level focus ring should be added to compensate.

## Resume criteria

Resume the mount card after designSystem supplies the DS button, shared historical
record lookup and frozen card presentation. Then wire the mutually exclusive
desktop/mobile entry points, sheet coordination, polite announcements and source
chat integration; run the requested unit, lint, typecheck and browser checks;
and capture the full light/dark screenshot matrix from the actual mounted UI.

No screenshots or feature-validation results are claimed by this handoff. The
feature remains incomplete, so this change must not close #4725. The external
loop retains ownership of commits, pushes and the ready-for-review PR lifecycle.
