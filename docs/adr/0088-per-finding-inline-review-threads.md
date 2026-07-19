# 0088 — Per-finding inline review threads: finding-granular GitHub-native memory

- Status: Proposed
- Date: 2026-07-19
- Builds on: ADR 0077 (findings convention — the objects this gives a lifecycle),
  ADR 0078 (QA tier; D5 "GitHub is the store" is the philosophy this extends from
  verdict granularity to finding granularity), pr-reviewer-plugin v0.4.0 (in-diff
  confinement — the prerequisite that makes line-anchored publication safe).
- Harvest source: `langchain-ai/open-swe`'s reviewer (agent/review/{findings,publish,
  reconcile}.py, read 2026-07-19) — the findings-lifecycle half of that system; its
  prompt-discipline half landed separately (#2063, follow-ups).

## Context

The QA panel posts ONE review per head: a marker line (machine), a prose brief, and a
fenced findings JSON (ADR 0078 C). Memory is verdict-granular: on a new head the
dispatcher recalls the last marker body's JSON as `prior_findings` and the panel
re-evaluates. That shape has served the shadow phase, but it leaves four gaps, all of
which open-swe's reviewer solves with one structural move — findings published as
individual inline review comments, each carrying a machine marker, reconciled against
GitHub thread state on every subsequent run:

1. **Author UX.** A monolithic comment can't be resolved piecemeal; authors can't
   mark finding 2 fixed while arguing finding 3. GitHub's native unit of review
   conversation is the inline thread, and we don't produce any.
2. **Resolution is unmeasurable.** The eval (`eval.py`, ws-91a) wants per-finding
   resolution — "do findings reach the verdict / get fixed?" — and today nothing
   records what happened to an individual finding between heads.
3. **Human replies are invisible.** When an author replies "this is intentional,
   see #123" on our verdict comment, no machinery ever reads it. open-swe captures
   the latest human reply per finding thread and routes it into a reassessment flow.
4. **The unresolved-threads promotion gate counts blind.** approve-on-green already
   holds on unresolved threads (ADR 0078), but our own findings never become
   threads, so the gate only ever counts OTHER reviewers' threads.

The `promotion`/`reaffirm` machinery, the pure verdict mapping, and the fail-closed
panel posture (ADR 0078 D3) are unchanged by this ADR — the model still never posts,
promotes, or chooses a verdict.

## Decision

**D1 — Findings publish as inline review comments.** The dispatcher (not the model)
publishes each surfaced finding as an inline comment on the PR review it already
posts: anchored to `file`+`line` (RIGHT side), body = claim + evidence + severity,
plus a machine marker `<!-- protoagent-qa-finding {"id","fingerprint","file","line"}
-->`. The review body keeps the verdict marker + prose brief + full findings JSON
exactly as today — recall and the board gate parse nothing new. Line anchoring is
safe because v0.4.0's confinement already guarantees surfaced findings sit on changed
paths; a finding whose line GitHub rejects degrades to the body (never lost, never a
crashed post).

**D2 — Surfacing cap.** At most 6 findings become inline threads, severity-ordered
(blocker → major → minor; nits never). The rest stay in the body report. open-swe's
cap; the noise-discipline rationale of ADR 0078 D4 applied to comment count.

**D3 — Reconciliation at dispatch time.** Before a delta re-review, the dispatcher
fetches the PR's review threads (one GraphQL query, extending the existing
unresolved-threads read) and matches our markers: a thread the author resolved or
GitHub outdated marks that finding `resolved` in the recalled `prior_findings`; the
latest human reply on our thread rides into the recipe wrapped as untrusted data.
GitHub remains the only store — richer recall, still no local review DB to drift
(ADR 0078 D5, upheld).

**D4 — Thread settlement is code, replies are panel text.** When a delta review
finds a prior finding fixed (the panel's JSON says `status: "resolved"` with a
`note`), the dispatcher posts the note as the thread reply and resolves the thread
via GraphQL `resolveReviewThread`. When a human reply disputes a finding and the
panel's re-evaluation agrees (`status: "dismissed"` + note), same mechanics. The
ADR 0077 contract gains two optional fields — `status` (`open`/`resolved`/
`dismissed`, default `open`) and the existing `note` doing reply duty — parsers
ignore unknown fields, so this is additive.

**D5 — Suggestion blocks, capped.** A finding whose fix is ≤4 lines and obvious may
carry `suggestion` text; the dispatcher renders it as a fenced ```suggestion``` block
(GitHub's commit-suggestion button). Longer suggestions are dropped at publish (a
review is not a rewrite); the finding still posts description-only.

**D6 — Phasing.** P1: D1+D2 (publish + markers, shadow repos). P2: D3 (reconcile +
thread-aware recall). P3: D4 (settlement + human-reply reassessment; needs the
`pull_request_review_comment` webhook event added to the chokepoint's dispatch set).
P4: D5. Each phase ships behind the shadow posture and earns the next via the
three-way eval, same as ADR 0078's stages.

## Consequences

- The eval gets its resolution substrate for free: per-finding outcomes become
  readable from thread state, no new telemetry schema.
- The promotion gate's unresolved-threads count now includes our own findings — a
  PASS with our open blocker thread cannot auto-promote until the author settles it.
  This is a behavior tightening and is intentional.
- More GitHub writes per review (N comments + occasional thread mutations); bounded
  by D2's cap and the existing chokepoint.
- Marker parsing joins verdict-marker parsing as load-bearing surface — both live in
  the plugin, both need the same "ours only" author checks the reconciler applies.
- Implementation lands almost entirely in pr-reviewer-plugin (publish/reconcile
  modules + webhook event); core's only change is the two additive contract fields
  and a recipe sentence teaching the panel `status`/`note` on delta re-reviews.

## Alternatives considered

- **Local findings DB** keyed by fingerprint — rejected: re-introduces exactly the
  drift ADR 0078 D5 removed; GitHub thread state is authoritative and already
  operator-visible.
- **Status quo** (verdict-granular body only) — rejected on the four gaps above;
  the shadow-phase eval cannot measure resolution without per-finding state.
- **Letting the panel post its own comments** (open-swe's shape — the agent calls
  `publish_review`) — rejected: our deterministic seam (model reviews, code posts)
  is the QA tier's core reliability property and stays.
