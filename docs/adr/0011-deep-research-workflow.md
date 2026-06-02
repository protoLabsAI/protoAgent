# ADR 0011 — Deep-research workflow with adversarial review

- **Status:** Accepted (2026-06-01) — design + implementation (recipe + roles ship with this ADR)
- **Date:** 2026-06-01
- **Deciders:** Josh Mabry; protoAgent maintainers
- **Tags:** research, workflows, subagents, adversarial, orchestration
- **Supersedes / Superseded by:** —

> Accepted. A single `researcher` subagent — even with the upgraded
> deep-research prompt (ADR-adjacent) — caps out: a live eval produced a
> well-cited but **one-sided, consensus-shaped** report with `Confidence: high`
> and **zero adversarial pressure**, and its gap-filling is an invisible
> in-prompt self-loop. To get a *complete, balanced* report we make deep
> research a **deliberate `run_workflow` recipe** (ADR 0002) that orchestrates
> discrete subagent stages — including a real **antagonist** (steelman the
> opposing case, red-team weak claims, hunt disconfirming evidence) and a
> **claim verifier** — feeding a synthesizer that must *address* the opposition.
> The single researcher stays for quick/inline lookups; the workflow is the
> deliberate path.

---

## 1. Context & Problem Statement

The upgraded `researcher` subagent decomposes into dimensions, cites `[N]`, and
rates confidence — good for a quick answer. But a live run (*"approaches to
sandboxing agents that run untrusted code + tradeoffs"*) exposed the ceiling of
a single agent:

- **One-sided.** It asserted a consensus ("containers insufficient", "MicroVMs
  strongest") with **no steelman of the counter-view** and **no disconfirming
  evidence** — yet rated itself `Confidence: high`. Unearned.
- **No verification.** Specific claims (e.g. latency figures) weren't checked
  against their sources.
- **Opaque, bounded gap-filling.** Gap detection is the agent's *internal*
  self-loop — not auditable, and limited by one agent's turn budget + context.

A "more complete report with clear opposing-agent context" needs **separation of
concerns across agents**: a gatherer shouldn't grade its own homework, and the
opposing case needs a dedicated advocate, not a footnote the same agent writes.

## 2. Decision

Ship a bundled **`deep-research` workflow** (`workflows/deep-research.yaml`) — a
fixed DAG of subagent stages with real parallel branches — plus three new
subagent roles. The engine (ADR 0002) is static-DAG with `steps.<id>.output`
template threading + parallel independent steps; we use that, not dynamic fan-out.

### 2.1 The stage graph

```
research ─┐                          (scope+gather, primary/recent sources)
dissent  ─┤  (parallel)              (deliberately hunt the critical/contrarian angle)
          ├─→ gap_fill               (find 1-3 genuine gaps vs the original Q, fill them)
          │      ├─→ antagonist ─┐   (steelman opposite + attack weak claims + seek disconfirming evidence)
          │      ├─→ verify ─────┤   (extract key claims, check vs sources, label supported/uncertain)
          └──────┴──────────────→ synthesize   (balanced report: addresses opposition, drops unverified claims)
```

- **research ∥ dissent** run in parallel from the topic — opposing context is
  injected *at gather time*, not bolted on.
- **antagonist ∥ verify** run in parallel after the evidence is in.
- **synthesize** depends on everything and is the deliverable.

### 2.2 New subagent roles
- **`antagonist`** (the headline) — full red-team: argue the strongest *opposing*
  position, attack weak/unsupported claims in the findings, and **search for
  disconfirming evidence** (its own `web_search`/`fetch_url`). Outputs an
  "Opposition & weaknesses" memo the synthesizer must answer.
- **`verifier`** — extract the key factual claims and check each against sources;
  label **supported / unsupported / uncertain** with citations. The synthesizer
  drops or flags anything not supported.
- **`synthesizer`** — write the balanced report: integrate findings + filled
  gaps, **incorporate a "Counterpoints / caveats" section** from the antagonist,
  keep only verifier-supported claims, numbered `[N]` citations, honest
  `Confidence`, open questions. May `memory_ingest` one durable finding.

The existing **`researcher`** handles the `research`, `dissent`, and `gap_fill`
steps (its prompt already scopes + cites). Stays available for quick inline use.

### 2.3 Why a workflow, not a bigger prompt
Separation across agents is the point: the antagonist is *adversarial by role*
(can't be the same agent that wrote the findings), the verifier is independent,
and each stage is its own auditable card (per-step output in the console).
Parallel branches keep wall-clock down.

## 3. Constraints / honest edges
- **DAGs, not loops** (ADR 0002): gap-filling is **one bounded stage**, not
  unbounded iteration — the declarative version of the loop. A deeper loop would
  need engine support (or goal-mode wrapping a workflow — itself an open gap from
  ADR 0009).
- **No dynamic per-dimension fan-out**: the recipe has fixed stages, so
  per-dimension parallelism lives *inside* the researcher (its prompt), while the
  workflow parallelizes the *role* branches (research∥dissent, antagonist∥verify).
- More subagent calls = more latency + tokens than a single researcher — this is
  the deliberate/complete path, not the quick one. Run it via
  `run_workflow("deep-research", {topic, depth})`.

## 4. Consequences
**Positive** — reports carry steelmanned opposition + verified claims + filled
gaps; the adversarial separation is structural (a role, not a prompt aside); each
stage is auditable; parallel branches bound wall-clock.
**Negative** — heavier than the single researcher (gate to genuinely deep asks);
bounded (single) gap round; new roles to maintain.

## 5. Alternatives considered
- **Keep improving the single researcher prompt** — hits the self-grading
  ceiling the eval exposed; an agent steelmanning against itself is weak.
- **Dynamic fan-out per dimension** — needs an engine `for_each`/map step; deferred
  (the role-branch parallelism covers most of the win).
- **Goal-mode loop until a verifier passes** — the right shape for *unbounded*
  gap-filling, but goals can't verify workflow output yet (ADR 0009 gap). Revisit.

## 6. Related
- [ADR 0002 — Reusable Subagent Workflows](/adr/0002-reusable-subagent-workflows) — the engine + recipe format.
- [ADR 0009 — Studio control stack](/adr/0009-studio-control-stack) — workflow = orchestration; the goal→workflow bridge that would enable a true gap loop.
- `graph/subagents/config.py` (roles), `workflows/deep-research.yaml` (recipe),
  `graph/workflows/engine.py` (DAG + threading). Lessons from `rabbit-hole.io`'s
  deep-research pipeline (scope → loop → synthesize + graph memory).
