# Reusable workflows

A **workflow** is a reusable, multi-step recipe over your subagents: research →
extract angles → write a brief, each step feeding the next, some running in
parallel. Define it once as YAML, run it many times with different inputs. See
[ADR 0002](/adr/0002-reusable-subagent-workflows) for the design.

## Skills vs Workflows

These get conflated — the console lists the skill index ("Skills") next to
"Workflows" — but they're two different altitudes (ADR 0009):

| | **Skill** ([`SKILL.md`](/guides/skills)) | **Workflow** (`*.yaml`) |
| --- | --- | --- |
| What | methodology — *how/when* to approach a task | a DAG of subagent steps with dependencies |
| How it runs | **retrieved** by relevance + **injected into context**; the model reads it and decides whether/how to follow | **executed** by the engine — fixed steps, deterministic |
| In control | the model (advisory) | the engine (orchestrated) |
| Trigger | automatic (semantic retrieval mid-turn) | explicit (run by name) |

Rule of thumb: a **skill advises**, a **workflow runs**. Reach for a skill when the
model should *know how* to do something; a workflow when you need *the same steps,
the same way, every time*.

## Anatomy

```yaml
name: research-and-brief            # unique slug (the lookup key)
description: Research a topic and write a cited brief.
inputs:
  - name: topic
    required: true
  - name: depth
    default: deep
steps:
  - id: gather
    subagent: researcher            # a key from SUBAGENT_REGISTRY
    prompt: "Research {{ inputs.topic }} ({{ inputs.depth }}). Find 3–5 sources."
  - id: angles
    subagent: researcher
    depends_on: [gather]
    prompt: "From this research, list the 3 key angles:\n{{ steps.gather.output }}"
  - id: brief
    subagent: researcher
    depends_on: [gather, angles]
    prompt: "Write a cited brief on {{ inputs.topic }}.\n{{ steps.gather.output }}\n{{ steps.angles.output }}"
output: "{{ steps.brief.output }}"  # optional; default = last step's output
```

- **Templating** substitutes `inputs.<name>` and `steps.<id>.output` (double
  curly braces). References are validated against the declared inputs + step ids.
- **`depends_on`** forms a DAG. Steps whose dependencies are ready run **in
  parallel** (bounded by `subagents.max_concurrency`), so latency ≈ the critical
  path. A step failure is recorded inline so independent branches still finish.

## Where recipes live

- **Bundled examples**: the repo's `workflows/` dir (ships with
  `research-and-brief.yaml` and `deep-research.yaml`).
- **Your recipes**: `workflows.dir` in `config/langgraph-config.yaml` (default
  `/sandbox/workflows`, falling back to `~/.protoagent/workflows` for local dev).
  Drop a `*.yaml` recipe there; it's loaded on the next start/reload.

## Running one

The lead agent has a **`run_workflow(name, inputs)`** tool:

- "run the research-and-brief workflow on quantum error correction" →
  `run_workflow("research-and-brief", {"topic": "quantum error correction"})`.
- An empty name lists the available workflows and their inputs.

Workflows only delegate to **configured subagents** (each with its own tool
allowlist and turn cap), and subagents don't get `run_workflow` — so there's no
recursion and the blast radius is exactly the subagent system's.

### As a slash command

Every registered workflow is also runnable straight from the chat composer as
**`/<workflow-name>`** — it autocompletes (the server lists workflows in
`GET /api/chat/commands`) and short-circuits the turn, returning the workflow's
output instead of a normal model reply. Arguments map to the recipe's inputs:

- `` /research-and-brief quantum error correction `` — free text fills the first
  required input (`topic`).
- `` /research-and-brief topic="quantum error correction" depth=shallow `` —
  explicit `key=value` tokens (quotes respected) set named inputs.

Missing a required input returns a `⚠️`-prefixed error naming it.

Each step streams its own tool card (e.g. `research-and-brief · gather` →
`· angles` → `· brief`) so a multi-step workflow shows live progress instead of
one opaque card.

## From the operator console

The React console has a **Workflows** surface (the rail icon next to Subagents).
It lists every registered recipe, shows the selected recipe's step DAG and its
inputs, and runs it with a one-click form — the same path the agent's
`run_workflow` tool takes. The result panel shows the final output plus a
collapsible per-step breakdown, and flags any steps that failed (failures are
recorded inline so the rest of the DAG still runs). It's backed by
`GET /api/workflows` and `POST /api/workflows/{name}/run`.

**Author one from the console** — the surface's **＋** opens a builder: name +
inputs + steps (id, a subagent picker, prompt, and `depends_on` checkboxes for
ordering/parallelism) + output. Saving validates the recipe against the live
subagent registry + DAG and writes it to the workflows dir (immediately
runnable) — `POST /api/workflows`; `DELETE /api/workflows/{name}` removes it.
Same `WorkflowRegistry.save` path the agent's `save_workflow` tool uses.

## Deep research with adversarial review

`deep-research.yaml` is the heavyweight, decision-grade counterpart to a single
`researcher` call. It's a six-stage DAG ([ADR 0011](/adr/0011-deep-research-workflow))
that builds in the *opposing* context a lone researcher can't give itself:

```
research ∥ dissent  →  gap_fill  →  antagonist ∥ verify  →  synthesize
```

- **research ∥ dissent** — a mainstream gather and a deliberately contrarian
  pass run in parallel, so the critical angle is sourced *at gather time*.
- **gap_fill** — finds and fills 1-3 genuine gaps against the original question.
- **antagonist ∥ verify** — a red-team [`antagonist`](/guides/subagents) (steelmans
  the opposing case, attacks weak claims, hunts disconfirming evidence) and an
  independent [`verifier`](/guides/subagents) (labels material claims
  supported/unsupported/uncertain) run in parallel.
- **synthesize** — a [`synthesizer`](/guides/subagents) writes the balanced
  report: it *addresses* the opposition in a "Counterpoints & caveats" section,
  drops unverified claims, and only earns `Confidence: high` if the opposition
  was genuinely answered.

Run it on a genuinely deep question — `` /deep-research is X the right
architecture `` or `run_workflow("deep-research", {"topic": "…", "depth": "deep"})`.
It's more subagent calls (and latency) than the single researcher, so gate it to
asks that warrant it; use the plain `researcher` for quick inline lookups.

## The agent can author them (closed loop)

The lead agent also has **`save_workflow(name, description, steps, inputs?, output?)`**.
Once it's worked out a multi-step process ad-hoc (via `task` / `task_batch`), it
can capture it as a reusable recipe — *"save that as a workflow called
competitor-scan"* — which is validated, written to `workflows.dir`, and
immediately runnable via `run_workflow`. This generalizes `skill-v1` emission
(a single-subagent recipe) to multi-step flows.

## Related

- [ADR 0002 — Reusable Subagent Workflows](/adr/0002-reusable-subagent-workflows)
- [Configure subagents](/guides/subagents)
- [Starter tools](/reference/starter-tools) — `run_workflow` lives alongside `task` / `task_batch`
