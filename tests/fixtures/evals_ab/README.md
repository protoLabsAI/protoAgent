# Bad-memory A/B fixture (ADR 0126 D6)

The learned-context A/B (`scripts/evals_ab.py --ab-learned-from <instance>`) runs the
eval suite twice: **arm A** on the bundled template only, **arm B** on the template
*plus a copy of a source agent's learned memories and private skills*. If the learned
context helps, B scores higher; if it is stale or poisoned, B scores **lower**.

This fixture proves the comparison detects **harm**, not just help.

## The seeded bad memory

[`bad_memory.json`](./bad_memory.json) is one false "learned" fact — the kind that
accretes in a long-lived agent's memory and then quietly contradicts reality:

> primary deployment region is **ap-south-1**

The contradicted eval case (`memory_region_recall`) expects **eu-west-1** and forbids
the stale value. The seed is written in the harness's own `kb_ingest` shape (the
`setup` block an `ask` case uses), so it is exactly a memory chunk arm B would be
seeded with and retrieve on that turn.

## The two arm reports

[`ab-A-9000.json`](./ab-A-9000.json) and [`ab-B-9000.json`](./ab-B-9000.json) are
fake runs of a four-case suite (frozen `ts=9000`, no live model):

| Case | Tier | Arm A | Arm B |
|---|---|---|---|
| `card_discovery` | gate | pass | pass |
| `memory_region_recall` | gate | **pass** | **fail** ← poisoned by the bad memory |
| `deploy_runbook_recall` | gate | pass | pass |
| `research_balance` | tracked | pass | pass |

Feeding both to `evals.compare.compare_ab` yields a **gate regression** (3/3 → 2/3)
and a single **regression flip** — `memory_region_recall`, A pass → B fail — carrying
the `trace_id` of both runs so the two traces open side by side. That flip is the
visible, test-covered proof that a bad memory shows up as harm in the A/B.

Pruning the bad memory is out of scope: the A/B's job is to **measure** the harm so a
human can decide. See `docs/guides/evals.md`.
