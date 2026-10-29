# ADR 0126 — Close the eval loop: nightly gate, feedback scores, traces become cases

- Status: Proposed
- Date: 2026-10-29
- Deciders: Josh Mabry; protoAgent maintainers
- Implementation: epic #4599 (E1 #4600, E2 #4601, E3 #4602, E4 #4603); not started.
- Builds on: [ADR 0006](./0006-observability-and-the-self-improving-flywheel.md) (measure → surface → advise),
  [ADR 0012](./0012-eval-strategy-and-model-comparison.md) (side-effect-verified harness, sweeps, judge),
  [ADR 0107](./0107-unified-self-improvement-policy.md) (self-improvement policy).
- Sources: Harrison Chase, "How to Build Agents That Improve Over Time" (Interrupt London, 2026-10-27);
  Hugging Face, "Training Agents IRL" workshop (2026-10-26).

## Context

An agent improves along three axes: the **model** (weights), the **harness** (code, prompts,
tools) and the **context** (memory, skills, knowledge). All three loops run on the same two
inputs: **traces**, which show what happened, and **evals**, which say what good looks like and
catch regressions while you change things. "Evals are the new PRD."

protoAgent has both inputs, but nothing connects them.

| Piece | Exists | Missing |
|---|---|---|
| Traces | Langfuse fleet-wide; `trace_id` on every per-turn telemetry row (`server/turn_telemetry.py`) | — |
| Scores on traces | `tracing.score_current_trace()` | **No callers.** It also can't score a past turn, because it only scores the active trace |
| Human feedback | — | No thumbs up/down anywhere in the console |
| Friction | `plugins/friction` ledger: `harness` and `model` kinds, severity, triage | Entries carry no `session_id` or `trace_id`, so a friction report can't be joined to the turn that produced it |
| Evals | 37 cases in `evals/tasks.json` (audit log, reply, KB, 8 rubric cases), sweeps, trend report | **Last run 2026-08-19.** Not scheduled, not in CI. Every case is hand-written, and all are core-generic: no fleet agent (jobCoach, necroMunda, protoEngineer) has cases of its own |
| Trace → case | — | A failed or exemplary turn can't become a regression case |

The two talks agree on order. You can only improve what you can measure, and the measurements
that matter come from real traffic. Training a model (SFT, distillation, RL) is the most
expensive loop and depends on the cheaper ones being in place first: realistic inputs from
traces, verifiers that check end state, and a clean split between eval and training data. This
ADR builds that foundation and nothing more.

## Decision

### D1 — Three tiers of eval, kept apart

Each tier makes a different claim (Mercor, "Training Agents IRL"):

| Tier | Claim | Cases | Grading | Use |
|---|---|---|---|---|
| **Gate** | "This still works" (conformity) | Deterministic cases: audit-log tools, reply patterns, KB side effects | Pass/fail, no model in the grader | Fails the nightly run |
| **Tracked** | "How well, how often" (frequency) | `verify_rubric` cases, `--repeat N` sweeps | LLM judge with a pinned grader model | Trend only, never a gate |
| **Red-team** | "This can go wrong" (existence) | Adversarial or perturbed inputs | One counterexample is enough | Later; not in this ADR |

`tasks.json` cases gain an optional `tier` field (`gate` or `tracked`). It defaults to `gate`
when no `verify_rubric` is set and to `tracked` when one is. The runner reports the tiers
separately, and the gate's pass rate is the number that matters.

### D2 — The suite runs nightly on the host, not in GitHub Actions

Hosted runners can't reach the model gateway, and putting gateway keys in a public repo's CI is
not worth it. A scheduled job on the dev host runs `python -m evals.sweep` against a throwaway
instance (ADR 0004, `PROTOAGENT_INSTANCE`), so it never touches a live agent's data. It uses the
model the default config names.

- Reports keep landing in `evals/results/`; `evals.report` keeps showing the trend.
- A gate regression (a case that passed the previous night and failed tonight) records a
  `harness` friction entry with the case id and the run file. That is the existing path to a
  human, so there is no new notification channel.
- Each case's report row records the `trace_id` of the turn it drove, where the agent returns one,
  so a failure links straight to its Langfuse trace.

### D3 — Feedback becomes a score on the turn's trace

- The console's assistant-message actions get thumbs up and thumbs down, with an optional
  one-line note.
- `POST /api/feedback {session_id, trace_id, value, note}` stores a row next to the turn's
  telemetry and writes a Langfuse score named `user_feedback` (1 or 0) on that `trace_id`.
- A new `tracing.score_trace(trace_id, name, value, comment)` scores a past trace by id.
  `score_current_trace` stays for in-turn scoring.
- Incognito turns are not scored. Feedback without a `trace_id` (tracing off) is still stored
  locally.

### D4 — Friction entries join their turn

`record_friction`, auto-captured friction and `record_service` stamp `session_id` and `trace_id`
on the ledger entry. Inside tool bodies they must use the injected session and trace
(`PROTO.md`, "Tool / state injection"), not `current_session_id()`, which is empty there. When a
`trace_id` is present, recording also writes a Langfuse score `friction` (0, comment = summary,
kind and severity) on that trace. Model-kind friction becomes a filterable set of labelled
traces, which is the raw material for both new cases (D5) and any later training data.

### D5 — A turn can be promoted to an eval case, and cases can live with the instance

- **Per-instance cases.** The runner loads core `evals/tasks.json` plus
  `<config dir>/evals/cases.json` when that file exists. Domain agents get suites of their own
  without forking `evals/`, and the instance config is already where per-agent behaviour lives.
- **Promote.** An operator action on an assistant turn (console message menu, or a friction
  entry in the friction view) drafts a case:
  - `prompt` is the user message that started the turn.
  - For a turn rated up, `expected_tools` are the tools the audit log shows fired for that
    `trace_id`.
  - For a turn rated down or carrying friction, the draft has no expectations. The operator
    writes what should have happened: tools, forbidden tools, reply patterns or a rubric.
    That expected behaviour is the requirement the case encodes.
  - Drafts are shown for editing and only written on save. Nothing is added to a suite without
    a human.
- Promoted cases record their source `trace_id`. The trace is evidence; the case is the
  requirement.

### D6 — Out of scope

- **Harness loop automation.** Pointing friction triage or ADR 0107's `/self-improve` at failing
  gate cases to propose fixes. It needs D2 and D4 first; file it when they land.
- **Context loop measurement.** A/B-ing the suite with and without `dream`/`distill` output to
  find out whether learned memories and skills help. Same dependency.
- **Training.** No SFT, distillation or RL is decided here. When it is considered, the starting
  points are narrow, high-volume decision points (tool-domain routing, friction classification,
  memory extraction), not the main agent. Two constraints already apply:
  1. Claude and OpenAI terms restrict training models on their outputs, so training data would
     have to come from open-model traces or human-written examples.
  2. An environment used for RL must first be checked for answer leakage, false passes and false
     fails, and must show at least one pass in 8 attempts. Reward hacking that an eval tolerates
     spreads through a training run.
- **Red-team tier.** D1 reserves the slot.
- **Langfuse datasets and experiments.** The local report stays the source of truth. Mirroring
  runs into Langfuse is an option once D2 runs reliably.

## Phases

All in epic #4599:

1. **E1 (#4600)** — Revive the suite and run it nightly: fix cases that rotted since 2026-08-19, add
   `tier`, the host schedule, the gate-regression friction entry, and `trace_id` in report rows (D1, D2).
2. **E2 (#4601)** — Feedback: console thumbs, `/api/feedback`, `tracing.score_trace` (D3).
3. **E3 (#4602)** — Friction joins its turn: `session_id`/`trace_id` on entries, the `friction` score (D4).
4. **E4 (#4603)** — Per-instance cases and promote-to-case from a turn or friction entry (D5). Needs E2
   for the rated-turn path and E3 for the friction path.

## Consequences

**Positive.** A regression shows up the next morning instead of whenever someone runs a sweep.
Operator judgement (thumbs, friction) lands on the trace, where it can be filtered, counted and
turned into cases. Domain agents get suites without forking. Every later loop (harness fixes,
memory measurement, training data) starts from labelled traces and a gate that says whether a
change helped.

**Negative.** The nightly run spends gateway tokens every day. The gate tier is cheap and
deterministic; tracked-tier cases cost a judge call each. Promoted cases need curating, or the
suite fills with near-duplicates. A flaky gate case trains people to ignore the nightly result,
so a case that flips twice in a week gets demoted to `tracked` until someone fixes it.

## Alternatives considered

- **Run evals in GitHub Actions.** Rejected for D2's reasons: no gateway reach from hosted
  runners, and keys in public CI.
- **Use Langfuse datasets and experiments as the case store.** That ties eval cases to one
  tracing backend that is optional in protoAgent, and the audit-log and KB checks need the
  local agent anyway. Mirroring stays possible later.
- **Auto-promote every thumbs-down to a case.** Without a stated expectation the case has
  nothing to assert, and the suite would grow with noise. A human writes the requirement.
- **Start with training.** It is the highest ceiling and the highest cost, and without D1–D5
  there are no labelled traces to train on and no gate to tell whether the trained model is
  better.
