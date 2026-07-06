# QA review takeover — harvest Quinn, ship a QA-engineer tier

- Date: 2026-07-06
- Status: Active (Phase A in flight)
- ADR: [`0078`](../adr/0078-fleet-pr-review-qa-tier.md)
- Predecessor program: [`codified-delivery-loop.md`](./codified-delivery-loop.md) (M1/M5
  built the review engine this program operationalizes)
- Source under harvest: protoWorkstacean's Quinn × protoPatch fleet-review system
  (writeup 2026-07-06; `workspace/agents/quinn.yaml`, `src/api/pr-inspector.ts`,
  `lib/plugins/github.ts` — 617 reviews/7d, 99.4% completion, 44.7s median)

## Objective

protoAgent-native agents take over Quinn's fleet-review role: auto-review every PR,
post formal verdicts, promote to merge deterministically — while keeping our structural
advantages (adversarial panel, parseable findings, closed fix loop) and adopting every
guard Quinn's production history paid for.

## The harvest (what her scar tissue taught, by issue number)

| Guard | Her evidence | Where it lands here |
|---|---|---|
| Blocking verdicts only on terminal CI; pending → one COMMENT, exit, never poll | #863 killed 24/25 of all failures | github-plugin verdict tools (server-side 409) |
| Approve-on-green is a pure function; models never "choose" to approve | #748, #888 (one decision fn, edge + sweep), #858/#903 (unresolved threads gate both paths), #901 (sweep scope) | pr-reviewer plugin policy layer |
| Structural trigger computed server-side, never eyeballed from a truncated diff | #891 (under-fired at ~5% → 61.4%) | pr-reviewer dispatch (recipe sizing); thresholds >3 files / >120 lines / sensitive-path regex, ported verbatim |
| Noise ledger: out-of-scope list + Gap-vs-finding + 80% bar + consolidation | "the field's highest-ROI noise lever" | core review-role prompts (Phase A) |
| Fail-closed exhaustion: a starved run never fabricates a promotable verdict | her operator-escalation rail; we hit the same hole live 2026-07-06 (two starved finders → partial report the gate would trust) | board gate reads the engine's `failed` list (Phase A) |
| GitHub-native memory: verdict body is the store; unchanged head → reaffirm; delta review | her prior_review flow | recipe `prior_findings` input + gate/dispatch carry |
| Dedup/cooldown chokepoint, typed drops | #437/#444/#459/#465 | pr-reviewer webhook dispatcher |
| Never review your own work | her self-approval rail | qaEngineer bundle policy (author/branch guard) |
| Don't build write-only flywheels | her unwired retrieval half | eval + learning wiring is IN scope per phase, or not built |

Kept from ours (not replaced by hers): the 4-angle panel + dedup + independent verify
(her ws-2uy wish), the ADR 0077 parseable findings (makes her open ws-91a — do findings
reach the verdict, per-finding resolution — trivially measurable), the board's
findings→coder bounce loop.

## Where it lives (the layering decision — ADR 0078 D1)

- **protoAgent core** — the review engine (ADR 0077 roles/recipe/findings). Gains only
  prompt hardening + a `prior_findings` recipe input. Stays domain-neutral.
- **github-plugin** — the hands: formal Review API tools (`github_review_approve` /
  `github_review_comment` / `github_review_request_changes`) with the CI-terminal guard
  enforced AT THE TOOL (Quinn's 409 pattern — unbypassable by the model), `github_path_exists`.
- **pr-reviewer-plugin** (new repo) — the deterministic machinery: webhook ingress +
  HMAC, the dedup/cooldown chokepoint, structural-trigger recipe sizing, prior-review
  recall, the approve-on-green pure function + 3-minute sweep (background surface),
  protoPatch client tool, the review eval. Composes github-plugin the way projectBoard
  composes delegates.
- **qaEngineer bundle** (new repo) — the agent: pins, archetype "QA Engineer", the
  ported persona (verdict system, three-layer verification, self-restriction), defaults
  (`shadow_mode: true`).

## Phases

**A — harden what we have** `protoAgent` + `projectBoard-plugin` [S, in flight]
- A1 core: noise ledger + Gap semantics + 80% bar + consolidation in
  review-finder/verifier/synthesizer prompts; `prior_findings` input on `code-review.yaml`
  (declared, default empty, threaded into finder prompts for delta re-reviews).
- A2 board: `_run_review_workflow` returns the engine's `failed` list; ANY failed finder
  ⇒ not-a-review (review-pending stays, reconcile retries) — a partial panel must never
  produce a promotable verdict. Gate stores last findings per fid and passes
  `prior_findings` on re-review.
**Accept:** a run with a starved finder never clears/bounces a feature; a bounce
re-review's finder prompts carry the prior findings.

**B — the verdict surface** `github-plugin` [M]
Formal Review API tools + `github_path_exists`; CI-terminal guard server-side in the
tool (pending/unknown/403 ⇒ refuse APPROVE/REQUEST_CHANGES with a comment-instead
message); self-review guard (author == token identity ⇒ refuse). Write-gated like
`github_merge_pr` (github.write).
**Accept:** unit tests prove a blocking verdict is impossible against pending CI and
against the agent's own PR.

**C — the reviewer machinery** `pr-reviewer-plugin` (new) [L]
Webhook route (HMAC) → chokepoint (30s cooldown per repo/PR/SHA, in-flight map, typed
drops) → dispatch: structural trigger picks full-panel vs lite recipe; run via
`STATE.workflow_run`; verdict mapping (findings → PASS/WARN/FAIL → review action);
fail-closed exhaustion (escalate via inbox/operator, never fabricate); approve-on-green
pure function + 3-min sweep as a background surface (promote COMMENTED→APPROVE only on:
all checks terminal-green ∧ zero unresolved threads ∧ dedup per head-SHA; fail closed on
every unknown); prior-review recall off the posted verdict body; protoPatch client
(optional structural step behind the trigger); eval script over telemetry (completion,
verdict mix, per-finding resolution — ws-91a answered).
**Accept:** an end-to-end dry run on a test repo: webhook → review → COMMENT → CI green
→ deterministic APPROVE → auto-merge armed, with every drop/skip typed and logged.

**D — the agent + shadow mode → cutover** `qaEngineer` bundle (new) [M]
Bundle pins + archetype + persona port + `shadow_mode: true` (COMMENT-only, no
promotion). Deploy alongside Quinn on the same repos; compare N weeks of findings vs
her verdicts + CodeRabbit threads + merge outcomes, using her own eval metrics as the
bar (respect the 44.7s-median question: measure where the multi-step panel earns its
latency vs her single pass; the lite recipe is the lever). Per-repo cutover: flip
`shadow_mode: false`, retire Quinn's dispatch for that repo, keep protoPatch as a
finding source throughout.
**Accept:** shadow report shows ≥ her catch-rate with acceptable noise on ≥2 repos;
one repo fully cut over with zero silent merges.

## Parked / explicit non-goals
- Rebuilding protoPatch (call the existing CLI/service; it's model-agnostic via gateway).
- The Qdrant learning loop — only wire retrieval WITH a consumer in the same phase
  (her write-only flywheel is the anti-lesson); our KG-lessons path is the substrate.
- bug_triage / security_triage skills — same harvest pattern, separate program.
