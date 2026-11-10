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

## Mount follow-up (2026-11-09, redo of bd-bqyfs)

The three original composite gaps above are now closed. The phone header is dropped
by Josh's scope change: `DecisionsHeaderButton` remains unused, and neither
`MobileShell.tsx` nor `mobile-shell.css` changes. Q13's spacer replacement and
Q23/Q24's new header button/focus ring are therefore no longer applicable.

The independent utility-bar mount, app-level polite announcement, and notification
panel routing are implemented. For Q15, App derives `sessionSheetOpen` with
`!panelOpen`, clears the session request before paint when Decisions opens, and
closes Decisions before accepting a session-sheet open. Both Drawers cannot render
open together, and closing Decisions cannot restore an old session-sheet request.

Three remaining contracts prevent completing the requested integration with the
existing composites:

- **Source-chat identity (r3, in-chat portion of r5):**
  `src/tools/lg_tools.py::request_decision` interrupts with `kind`, `question`,
  `options`, `context`, expiry and default fields, but no decision id.
  `src/server/chat.py` awaits `record_decision_pause` and discards its returned
  record before yielding the original payload. `record_decision_pause` in
  `src/server/turn_control.py` does not mutate that payload. Consequently the
  console cannot call `useDecision(id)` for this interrupt. The id must be emitted
  and retained in durable task history before the live and historical transcript
  paths can mount the card reliably. Do not guess identity from question text or
  substitute a chat-local decision record. `HitlDisplay.tsx` is left unchanged.
- **Mounted panel context (offline portion of r5):** `DecisionsPanel` accepts
  `agentOnline`, `onOpenChat` and `onReplyInChat`, but `DecisionsWidget` exposes no
  props and supplies none of these. Its cards default to online and its chat links
  have no callback. The widget needs to expose these existing panel contracts so
  the shell can supply presence and navigation. An offline screenshot of this
  mounted widget would currently misrepresent actual behavior.

- **Closed conflict copy (expired/withdrawn portion of r5):** The browser tests
  demonstrate that `DecisionCard.choose` maps every 409 to `answeredElsewhere`.
  In panel mode that always invokes `answeredElsewhereCallout`, even when the
  returned record is expired or withdrawn. Both cases incorrectly say "Already
  answered". The spec retains the required assertions as explicit expected
  failures until the prerequisite composite handles each closed state. No passing
  screenshot of these states is claimed.

The browser spec covers the reachable pill/dialog states, answer success and 409,
failed-send retry availability, notifications at desktop/tablet/phone widths, and
sheet exclusivity. `DECISION_SCREENSHOTS=1` captures reachable desktop states in
both themes under `apps/web/e2e/screenshots/decision-needed/`. Source-chat, offline, expired and withdrawn
screenshots are declined pending the contracts above. This partial mount must not
close #4725. The external loop still owns committing evidence, head-SHA raw image
URLs, and the ready-for-review PR lifecycle.

Validation of the partial mount: web unit tests passed (409 files, 4,918 tests),
web build/typechecks passed, and the browser spec completed with 14 passing cases
plus four explicit expected failures (expired/withdrawn in both themes). Fourteen
1440px PNGs capture quiet, empty, pending hover, three agents, sent, answered
elsewhere and failed-send states in light/dark. The repository gate passed ruff,
import contracts, attribution and lockfile checks. Its unsandboxed fail-fast Python
run stopped after 3,142 passes and 120 skips at
`test_the_vendored_embed_css_matches_the_installed_package`: the existing vendored
CSS header names `@protolabsai/ui-css@0.71.0`, while the lockfile installs 0.72.0.
That unrelated vendored asset is unchanged; a full Python gate pass is not claimed.
