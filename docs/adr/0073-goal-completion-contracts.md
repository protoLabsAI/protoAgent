# 0073 — Goal completion contracts

Status: **Accepted**

> Due-diligence framing: Hermes' **Judgment** (v0.18.0) ships a "completion contract"
> UX — an agent is given a structured statement of the required outcome, the constraints
> it must not violate, the boundaries it may act within, and a stop-condition — and then
> an **LLM judge** decides whether the contract is met. protoAgent already has the harder
> half (a **real, deterministic verifier** decides DONE; ADR 0028). What we lacked was the
> *contract UX* around it. This ADR adopts the contract shape as a structured layer OVER
> the existing verifier surface — **keeping deterministic verification as the arbiter**,
> which is our edge over an LLM-judge-as-arbiter.

## Context

A goal (`GoalState`, `graph/goals/`) is a testable outcome the agent self-drives toward:
after each terminal turn, a **verifier** (`graph/goals/verifiers.py` —
`command`/`test`/`ci`/`data`/`llm`/`plugin`) decides whether the goal is met; if not, the
`GoalController` re-invokes the agent with a **continuation prompt** until the verifier
passes or the iteration / no-progress budget is spent (ADR 0028, ADR 0030→0067).

Two gaps, both about *directing* the loop (not *deciding* it):

1. **The continuation prompt only carries the bare condition.** It re-states
   `"Keep working toward the goal: <condition>"` plus the last verifier reason and the
   running plan. It says nothing about what must **not** change, where the agent may act,
   or when it should **stop and ask** rather than push on. A long drive loop can therefore
   regress an invariant or wander out of scope while still chasing a green verifier.

2. **There is no first-class place to state the contract.** Forks hand-roll these
   directives into the free-text `condition`, which then also feeds the `llm` verifier —
   conflating *what to verify* with *how to behave while driving*.

Hermes-style tools answer this with an LLM judge over a free-text contract. We deliberately
do **not** — an LLM-judge-as-arbiter is exactly the "protocli free-text-LLM-judged goals"
posture our `GoalState` docstring contrasts us against. The verifier stays authoritative.

## Decision

Add an optional, structured **completion contract** to `GoalState` — a directive layer
that shapes the continuation prompt each drive turn, while the **verifier remains the sole
arbiter of DONE**.

### D1 — Four contract fields on `GoalState` (all default-empty)

```
outcome:     str        # the single required end-state (human summary; falls back
                        # to `condition` when empty — see `resolved_outcome`)
constraints: list[str]  # invariants the agent must NOT violate/regress
boundaries:  list[str]  # the files/dirs/systems in scope (stay inside these)
stop_when:   str        # a condition under which the agent should PAUSE and ask
```

All default-empty (`""` / `[]`), so a goal set without a contract is byte-for-byte
unchanged in storage and behavior. `verifier` is untouched — it stays the contract's
**verification** (the real check). `to_dict`/`from_dict` (via `asdict` + forward-compatible
key filtering) carry the fields for free; `status_line` appends a `contract` marker when any
field is set.

### D2 — Continuation-prompt injection (contract-directed continuation)

`GoalController._contract_prompt(state)` renders the **non-empty** fields into one compact
directive block, e.g.:

> Contract for this goal: it is DONE only when the verifier passes (command: pytest -q).
> Required outcome: the suite is green on main. Constraints (do NOT violate): no new
> network calls; public API unchanged. Stay within these boundaries: graph/goals/. If the
> fix would require a schema migration, STOP and ask the operator instead of continuing.

It is appended to the existing continuation text every drive turn (both same-session and
fresh-context loops). This mirrors Hermes' contract-directed continuation — but it is
**directive only**: the DONE decision still comes from the verifier in `evaluate`. A goal
with no contract renders `""`, so nothing is appended and the prompt is unchanged.

### D3 — Verification stays a real verifier (the edge)

We do **not** add an LLM judge as the completion arbiter. `evaluate` still runs the goal's
verifier first and treats its result as ground truth (it even overrides a same-turn
`abandon_goal` give-up). `llm` remains an **opt-in verifier type**, never the default
arbiter. The contract enriches *how the agent is steered*, never *how completion is judged*.

### D4 — `stop_when` v1 is prompt-injected (self-park), not auto-detected

`stop_when` is rendered into the directive block; the agent self-parks via the existing
`abandon_goal` / ask path when it recognizes the condition. We do **not** build
auto-detection of `stop_when`. A `# follow-up:` comment in `controller.py` notes the
optional future: a `stop_when`-as-verifier that could park the loop deterministically
(reusing the verifier surface), for when a stop-condition is mechanically checkable.

### D5 — Plumbed through the operator set-path only (no new UI here)

`set_goal_operator` and `set_goal_safe` accept the fields as optional keyword args;
`POST /api/goals` (`_operator_goals_set`) accepts optional `outcome` / `constraints` /
`boundaries` / `stop_when`, validating types (a string sent for a list field is coerced to
a 1-element list; unknown/other types ignored). Backward-compatible: a body with only
`{session_id, condition, verifier}` still works. The TS `setGoal` body and `GoalState`
types gain the optional fields so a later goal-creation form can send them and the
GoalsPanel can surface them. **This ADR is backend-only**; the FE goal-creation form is a
tracked follow-up.

## Consequences

- **Better-directed drive loops** without loosening the completion bar: the agent is
  reminded of invariants, scope, and its stop-condition every turn.
- **A clean separation** of *what to verify* (`verifier`) from *how to behave while driving*
  (the contract) — forks stop overloading `condition`.
- **No new arbiter risk.** Because verification is unchanged, none of the LLM-judge failure
  modes (prompt-injection of the judge, judge drift) enter the completion decision.
- **Follow-ups:** the FE goal-creation form (Part 2) **shipped** — a guided wizard (verifier
  cards + the contract step) in the console, with the contract read-back surfaced in a goal
  **detail drawer** (plan + timeline + contract). Still open: optionally a `stop_when`-as-verifier
  auto-park (D4).
