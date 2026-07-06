# 0077 — Adversarial code-review workflow + the findings convention

- Status: Proposed
- Date: 2026-07-06
- Builds on: ADR 0002 (declarative workflow engine — the DAG this recipe runs on),
  ADR 0011 (deep-research adversarial roles — the reviewed-by-strangers pattern this
  ports from prose to code), ADR 0027 (plugin recipe dirs), ADR 0064 (execution-grounded
  code-solve — the board loop that will consume this as a gate).
- Plan: [`docs/plans/codified-delivery-loop.md`](../plans/codified-delivery-loop.md)
  (M1; the board-gate consumer is M5).

## Context

The delivery loop's review step is the weakest link in the codified pipeline: the board
either merges on green CI alone or fires an *advisory* `review_dispatch` whose output
gates nothing. Meanwhile the repo's own practice — the reviews that caught real defects
in #1846/#1847 — is consistently the same shape: several narrow read-throughs of the
diff from different angles, a dedup, and a skeptical pass that kills the
plausible-but-wrong findings before they reach the author. That shape already exists in
the codebase for research (deep-research.yaml: gather ∥ dissent → antagonist ∥ verify →
synthesize), but not for code.

Two things were missing:

1. **A machine shape for findings.** A review that gates a merge must be parseable —
   "stored on the bead, injected into the retry prompt, counted against
   `review_fix_max`" all need structure, not prose. But workflow steps thread *text*
   (ADR 0002's engine is deliberately string-based), so the structure has to be a
   convention layered on prose, tolerant of LLM output.
2. **A review recipe.** Finder angles, dedup, verification, and rendering as a
   first-class workflow — callable by a human (`/code-review`) and headlessly by the
   board gate (`run_workflow("code-review", …)`).

## Decision

**D1 — One findings convention, one module.** `graph/review/findings.py` owns the
schema — `[{file, line, severity, category, claim, evidence, verdict?, note?}]`,
severities `blocker|major|minor|nit`, verdicts `confirmed|refuted|uncertain` — as three
artifacts: `FINDINGS_CONTRACT` (the prompt snippet producers embed, so the schema is
written down exactly once), `parse_findings` (tolerant JSON-in-prose extraction:
fenced block preferred, bare arrays accepted, junk items skipped, fields coerced,
verifier vocabulary like SUPPORTED/UNSUPPORTED normalized), and
`render_findings_markdown` (the one human-facing rendering). The engine stays
string-based; the contract lives at the edges. Consumers: the craft `/code-review`
skill, the projectBoard review gate (M5), the console.

**D2 — Two new roles, one reused.** `review-finder` reads the diff
(`github_pr_diff` / `github_get_commit_diff` / `github_read_file`) from ONE angle
passed in the step prompt — same separation-of-lanes reasoning as ADR 0011's roles:
a finder told to look for everything finds the shallow things. `review-synthesizer`
dedups/ranks/re-grades the merged list and never adds findings of its own. The
verify pass reuses the existing `verifier` role: its prompt gains a code-findings
mode (annotate each finding `confirmed|refuted|uncertain` + a note, never add/drop)
and its allowlist gains the github read tools — verdicts must come from re-reading
the actual diff, not from vibes about the findings' prose. Both new roles set
`allow_skill_emission=False` (per-invocation verdicts must not pollute the skills
index).

**D3 — The recipe: 4 finders ∥ → dedup → one verify pass → report.**
`code-review.yaml`: four static finder steps (correctness, removed-behavior,
cross-file, conventions) run in parallel; `review-synthesizer` merges; `verifier`
makes ONE pass over the merged list; a final `review-synthesizer` step drops the
refuted findings and emits the deliverable ending in the canonical fenced JSON
block. **Deliberately not** per-finding verify fan-out: the ADR 0002 engine's steps
are static, and a data-driven "spawn one verify step per finding" primitive is real
engine work. The single merged-list pass is the honest compromise — extend the
engine only if it demonstrably misses (parked in the plan).

**D4 — The skill drives the workflow.** The craft `/code-review` skill becomes a
thin driver: pin the PR, `run_workflow("code-review", …)`, present the findings
grouped by severity with verdicts visible. Its old two-axis inline review survives
only as the fallback when the workflows/github plugins aren't available.

## Consequences

- The board's M5 review gate consumes this unchanged: run the recipe headlessly,
  `parse_findings` the output, store on the bead, inject into the retry prompt,
  bound by `review_fix_max` — the same closed loop as the CI bounce.
- Four finder angles are static config in the recipe, not code — a fork adds a
  `security` finder by editing YAML (the `review-finder` role is already
  angle-generic).
- The verify pass is the cost center (~1 heavy step over the whole list). If it
  proves too coarse — refuting good findings or confirming bad ones in bulk — the
  parked engine primitive (data-driven fan-out) is the fix, not a bigger prompt.
- `verifier` now carries github read tools into research verifications too;
  missing-tool names resolve to nothing when the github plugin is off, so
  deep-research deployments without it are unaffected.
- Findings are only as anchored as the diff the finders fetched — a force-push
  mid-review yields verdicts about a stale diff. The gate (M5) re-runs on new
  pushes, same as CI.
