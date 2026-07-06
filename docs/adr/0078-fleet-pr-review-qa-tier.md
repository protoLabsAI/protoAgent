# 0078 — Fleet PR review as a protoAgent tier: harvest Quinn, keep the panel

- Status: Accepted (2026-07-06 — Phases A/B/B2/C shipped and accepted live; D built,
  shadow data collection is the standing posture; stage-2/3 promotion stays a per-repo
  operator decision earned by the three-way eval)
- Date: 2026-07-06
- Builds on: ADR 0077 (the adversarial review workflow + findings convention — the
  engine this operationalizes), ADR 0025 (delegates spine), ADR 0042 (bundle
  archetypes — how the QA engineer presents), ADR 0006 (flywheel — the eval/learning
  posture), projectBoard v0.30.0 (the board-tier review gate that shares the engine).
- Harvest source: protoWorkstacean's Quinn × protoPatch review system (production:
  617 reviews/7d, 99.4% completion; sources `quinn.yaml`, `pr-inspector.ts`,
  `github.ts` read 2026-07-06).
- Plan: [`docs/plans/qa-review-takeover.md`](../plans/qa-review-takeover.md).

## Context

The fleet's PR review runs on Quinn — a single-agent DeepAgent in protoWorkstacean
whose reliability comes from a specific discipline: every objective decision
(structural trigger, CI-terminal gating, approve-on-green, dedup, thread guards) was
moved out of the prompt into deterministic code, each move retiring a named failure
mode (#863, #748, #891, #858/#903). Meanwhile protoAgent grew a structurally stronger
review *engine* (ADR 0077): a four-angle adversarial panel with dedup and independent
verification, findings as a parseable convention, and a board gate that closes the loop
by bouncing findings into the authoring coder's retry prompt.

The two systems are complementary, not competing: Quinn has the operational guards and
the verdict/merge surface; we have the better reviewer and the fix loop. Taking over
her role means porting her guards around our engine — not rebuilding either.

## Decision

**D1 — Four-layer placement, matching the fleet's existing tiers.** The review engine
stays in **core** (ADR 0077 — domain-neutral). Generic GitHub verdict capability goes in
**github-plugin**: formal Review API tools (`github_review_approve` /
`github_review_comment` / `github_review_request_changes`) plus `github_path_exists`.
Role-specific machinery goes in a new **pr-reviewer-plugin**: webhook ingress, the
dispatch chokepoint, structural-trigger recipe sizing, the approve-on-green policy
layer + sweep, prior-review recall, the protoPatch client, and the review eval. The
agent itself is a new **qaEngineer bundle** (pins + archetype + persona + defaults),
the leadEngineer pattern applied to QA.

**D2 — Guards live below the model, verbatim from Quinn's ledger.** The CI-terminal
guard is enforced INSIDE the verdict tools (refuse APPROVE/REQUEST_CHANGES while any
check is non-terminal or unverifiable — the model receives a comment-instead error, so
the #863 busy-wait class cannot exist). Approve-on-green is a pure function used by
both the webhook edge and a 3-minute level sweep: promote COMMENTED→APPROVE only when
all required checks are terminal-green AND zero unresolved review threads AND not
already promoted for this head SHA; every unknown falls through fail-closed. The
structural trigger (>3 files, >120 changed lines, or a sensitive path —
auth/session/token/crypto/payment/billing/migrations/CI-CD/docker) is computed from
authoritative PR JSON + full-diff paths, never from a truncated diff, and selects the
recipe (full panel vs lite).

**D3 — Fail closed at every judgement boundary.** A review run with ANY failed panel
step is not a review: the caller (board gate, reviewer dispatch) must treat it as
unreviewed — retry or escalate to the operator — never synthesize a verdict from a
partial panel (a promotable verdict from a starved run is how an unreviewed PR
auto-merges; observed in our own stack 2026-07-06). A run that cannot verify an
external reference records a **Gap**, never a severity. The reviewer never reviews its
own PRs (author/branch guard in the bundle policy).

**D4 — The panel stays; noise discipline comes from the prompt ledger.** We keep the
ADR 0077 multi-angle panel + verifier (Quinn's own roadmap wished for this — ws-2uy)
and port her signal-to-noise rules INTO the role prompts as a deliberate
negative-prompting exception: the out-of-scope ledger (linter-owned style, theoretical
risks behind impossible preconditions, subjective preference, speculation about unread
code, already-resolved threads), the ≥80% confidence bar, and consolidation of
duplicate findings. Findings remain the ADR 0077 parseable convention — which makes
per-finding resolution rate (Quinn's open ws-91a) directly measurable.

**D5 — GitHub-native review memory.** The posted verdict is the store: unchanged head
⇒ reaffirm; advanced head ⇒ delta review carrying forward still-open findings via a
`prior_findings` recipe input. No local review store to drift.

**D6 — Shadow mode before cutover.** The qaEngineer bundle ships `shadow_mode: true`
(COMMENT-only, no promotion), running on the same PRs Quinn reviews. Cutover is
per-repo, judged against her own eval metrics (completion, verdict mix, latency,
catch-rate vs merge outcomes). protoPatch remains a finding source either way — called,
not rebuilt.

## Consequences

- Four PR surfaces evolve in step (core prompts, github-plugin tools, reviewer plugin,
  bundle); the findings convention (ADR 0077) is now load-bearing across all of them —
  schema changes need a compat sweep.
- The verdict tools are write-scoped and identity-bearing: which GitHub App/token the
  QA agent posts as (inherit `@protoquinn` vs a new identity) is an operational
  decision deferred to Phase D — shadow mode works under any identity.
- The multi-step panel costs minutes where Quinn's single pass costs ~45s median.
  The lite recipe + structural trigger is the cost lever; shadow mode measures where
  the panel earns its latency. If it doesn't on small PRs, the lite path must win there.
- Two new repos to operate (pr-reviewer-plugin, qaEngineer bundle) — accepted: it
  mirrors the proven leadEngineer/pm-stack shape and keeps github-plugin lean for its
  other consumers.
- Revisit triggers: shadow-mode report (Phase D accept); or the engine gaining
  data-driven fan-out (ADR 0077's parked per-finding verify), which would strengthen
  the panel's precision claim.
