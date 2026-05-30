# ADR 0002 — Reusable Subagent Workflows

- **Status:** Accepted (2026-05-30) — execution underway, slice by slice
- **Date:** 2026-05-30
- **Deciders:** Josh Mabry; protoAgent maintainers
- **Tags:** architecture, workflows, subagents, orchestration, skills, security
- **Supersedes / Superseded by:** —

> Accepted. protoAgent gains a **reusable, multi-step workflow** layer over its
> subagents: a workflow is a declarative YAML recipe (inputs + a step DAG, each
> step delegating to a subagent with a templated prompt that can reference
> inputs and prior step outputs). Delivered in slices: (1) engine + registry +
> `run_workflow` tool, (2) agent-emitted workflows, (3) operator console
> surface. Generalizes skill-v1 (a single-subagent recipe) to multi-step flows.

---

## 1. Context & Problem Statement

protoAgent can delegate to subagents two ways: **`task`** (one delegation) and
**`task_batch`** (a flat, single-layer fan-out — independent tasks run
concurrently, results concatenated). Neither is **reusable** or **multi-step**:
there's no way to define a flow once — "research → extract angles → write a
cited brief", where each step feeds the next — and re-run it with new inputs.

The closest reusable artifact, **skill-v1**, captures a *single* subagent recipe
(`prompt_template` + `tools_used`). It can't express a sequence or a fan-out
with downstream aggregation.

Claude Code's **Workflow** system is the reference: a named, parameterized
orchestration over a subagent pool with ordered **phases**, **pipeline**
(per-item multi-stage, no barrier) and **parallel** (barrier fan-out) stages,
**structured outputs** that compose, and deterministic control flow. We want
that *capability* — reusable multi-step orchestration — in protoAgent's idiom.

## 2. Decision

Add **declarative YAML workflow recipes** executed by a workflow **engine** over
the existing `_run_subagent` runner.

### Why declarative (not executable scripts)

Claude's Workflow uses JS scripts (full programmability). We choose a
**declarative recipe** instead because it matches protoAgent's existing ethos
(AgentSkills-style `SKILL.md` folders, config-as-data) and avoids an
`execute_code`-class security surface:

| | Declarative YAML (chosen) | Executable script |
|---|---|---|
| Safety | No code execution; only delegates to configured subagents | Runs author-written code |
| Authoring | Operators, the agent (emission), shareable files | Engineers |
| UI-manageable | Yes (structured) | Hard |
| Power | Sequential + parallel DAG, output threading | Arbitrary logic |

The declarative model covers the common case (research/synthesis pipelines,
fan-out-then-aggregate). An optional gated code-step escape hatch is a possible
*future* extension, not v1.

### Recipe schema (v1)

```yaml
name: research-and-brief            # unique slug
description: Research a topic and write a cited brief
version: 1
inputs:
  - name: topic
    required: true
  - name: depth
    default: deep
steps:
  - id: gather
    subagent: researcher            # a SUBAGENT_REGISTRY key
    prompt: "Research {{inputs.topic}} ({{inputs.depth}}). Find 3–5 strong sources."
  - id: angles
    subagent: researcher
    depends_on: [gather]
    prompt: "From this research, list the 3 key angles:\n{{steps.gather.output}}"
  - id: brief
    subagent: researcher
    depends_on: [gather, angles]
    prompt: "Write a cited brief on {{inputs.topic}}.\nResearch:\n{{steps.gather.output}}\nAngles:\n{{steps.angles.output}}"
output: "{{steps.brief.output}}"    # optional; default = last step's output
```

- **Templating** is plain double-curly substitution of `inputs.<name>` and
  `steps.<id>.output` (as shown above) — no logic. References are validated
  against the declared inputs + step ids.
- **`depends_on`** forms a DAG. Steps whose dependencies are satisfied run
  **in parallel**, bounded by `subagent_max_concurrency`. Total latency ≈ the
  critical path, not the sum.

### Execution model

1. **Validate**: unique step ids; `depends_on` references exist; no cycles;
   `subagent` is in the registry; required inputs supplied; templates reference
   only known names.
2. **Run** the DAG: each ready step renders its prompt (inputs + completed step
   outputs) and calls `_run_subagent(subagent_type=…, prompt=…)`; outputs feed
   dependents. Failures are recorded inline (the step's output becomes the error
   text) so independent branches still complete — matching `task_batch`'s
   non-aborting semantics.
3. **Return** the rendered `output` (default: the last step's output).

### Surfaces

- **`run_workflow(name, inputs)` tool** — the lead agent invokes a saved
  workflow. (Subagents never get this tool, so workflows can't recurse —
  delegation depth stays one level, as today.)
- **Registry**: recipes loaded from a writable `workflows/` dir
  (`/sandbox/workflows` → `~/.protoagent/workflows` fallback, mirroring skills).
- **Emission** (slice 2): a successful multi-step delegation is captured as a
  `workflow-v1` recipe and ingested into the registry — the closed learning
  loop, generalizing skill-v1.
- **Operator console** (slice 3): a Workflows surface to list / view / run.

### Security & limits

Declarative recipes execute no code. A step can only **delegate to a configured
subagent**, which keeps its own tool allowlist and `max_turns` cap. Step count
is capped; there's no workflow→workflow recursion in v1. So the blast radius is
exactly that of the existing subagent system.

## 3. Consequences

- **Good**: reusable, parameterized, shareable, agent-emittable orchestration;
  safe; UI-manageable; reuses the subagent runner + concurrency cap.
- **Trade-off**: no arbitrary in-workflow logic (conditionals/loops) in v1 — the
  DAG + templating covers the common cases; richer control flow is a follow-up.
- **Relation to skills**: skill-v1 stays the single-step recipe; workflow-v1 is
  the multi-step generalization. Both are agent-emittable artifacts.

## 4. Slices

1. **Engine + registry + `run_workflow` tool** — the core (this ADR's heart).
2. **Agent-emitted workflows** — `workflow-v1` capture + registry ingest.
3. **Operator console surface** — list / view / run workflows.

## Related

- [ADR 0001 — Extensibility & Plugin Architecture](./0001-extensibility-and-plugin-architecture.md)
- [Configure subagents](/guides/subagents) · [Starter tools](/reference/starter-tools)
- [Extensions reference](/reference/extensions) — the `skill-v1` artifact this generalizes
