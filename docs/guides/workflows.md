# Reusable workflows

A workflow runs a saved recipe of subagent steps, passing outputs between them.
Use one for a repeatable process such as research → review → write a brief.

## Run one (the short path)

1. Enable the bundled `workflows` plugin: add `workflows` to `plugins.enabled`
   without removing other entries, then reload configuration.
2. Open **Workflows**, choose a recipe, and fill in its inputs. The recipe's
   subagents must be [configured and available](/guides/subagents).
3. Run it and follow the step timeline. Inspect the final output and any failed
   or timed-out steps before relying on the result.
4. If it pauses for approval, review the pending step and prior outputs in
   **Pending Gates**, then approve, edit, or reject that step.

The plugin supplies the tools, API, and console surface. `workflows.enabled`
is not an enable switch; use `plugins.enabled`.

## Skills vs Workflows

A [skill](/guides/skills) supplies instructions the model can follow. A workflow
executes a declared sequence and dependencies. Choose a skill for guidance; use
a workflow when the steps must be repeatable. Subagent answers remain
model-generated, even when the step ordering is fixed.

## Anatomy

```yaml
name: research-and-brief
description: Research a topic and write a cited brief.
inputs:
  - name: topic
    required: true
steps:
  - id: gather
    subagent: researcher
    prompt: "Research {{ inputs.topic }}. Find 3–5 sources."
  - id: brief
    subagent: researcher
    depends_on: [gather]
    prompt: "Write a cited brief from this research: {{ steps.gather.output }}"
output: "{{ steps.brief.output }}"
```

- Templates substitute `inputs.<name>` and `steps.<id>.output`. References must
  name declared inputs and steps.
- `depends_on` sets ordering. Ready steps can run in parallel. The recipe's
  optional `max_concurrency` (1–16) overrides the default from
  `subagents.max_concurrency`.
- `output` defaults to the last declared step's output.
- A step error becomes inline output; other steps can continue, including
  dependents that receive that error. A completed final step does not prove
  that every earlier step succeeded.
- Optional `gate: human` pauses **before** dispatching that step. Approving runs
  it; editing runs it with the replacement prompt. Rejecting records an error
  for the step and continues the remaining graph; it does not cancel the run.

## Where recipes live

Bundled recipes live in `plugins/workflows/recipes/`. Enabled plugins can add
recipe directories. Your saved recipes live under `<instance_root>/workflows/`,
or an explicit non-`/sandbox` `workflows.dir` override. A saved recipe wins over
another recipe with the same name.

The registry reads `*.yaml` and `*.yml`. Reload configuration after editing files
by hand; recipes saved through the console or tool are available immediately.
See [workflow configuration](/reference/configuration#workflows) for storage and
run-history retention.

## Running one

Ask the agent to run a named recipe, for example:

> Run research-and-brief on quantum error correction.

The tool is `run_workflow(name, inputs)`; an empty name lists available recipes
and inputs. Steps use configured subagents with their tool allowlists and turn
limits. Subagents do not receive `run_workflow`.

### As a slash command

Type `/` in chat to find a recipe, then supply free text for its first required
input or use named arguments:

```text
/research-and-brief quantum error correction
/research-and-brief topic="quantum error correction"
```

Missing required inputs return an error. Steps stream tool cards, and the final
output replaces a normal model reply. A gated run returns its run id and resume
instructions instead.

## From the operator console

**Workflows** lists recipes, inputs, and their step graph. Use **New workflow**
to author a recipe or edit an existing one. Saving validates its dependencies,
subagents, and templates. Run history records outputs and per-step progress;
**Pending Gates** shows runs awaiting a decision. Resuming keeps completed step
outputs instead of running them again.

All routes below are beneath `/api/plugins/workflows`:

| Method | Path | Purpose |
| --- | --- | --- |
| `GET` | `/list` | Recipe summaries |
| `GET` | `/{name}/recipe` | Full editable recipe |
| `POST` | `/validate`, `/save` | Validate or save a recipe document |
| `DELETE` | `/{name}` | Remove the writable copy of a recipe |
| `POST` | `/{name}/start` | Start in the background; body `{inputs}`; returns `run_id` |
| `POST` | `/{name}/run` | Run synchronously and return the result |
| `GET` | `/runs/all`, `/runs/{run_id}` | Run history or a full run record |
| `GET` | `/runs` | Paused runs awaiting approval |
| `POST` | `/runs/{run_id}/resume` | Resume with `action: approve`, `edit`, or `reject` |

For an edited resume, supply `edits: {prompt: "replacement prompt"}`. The console
also sends `background: true` and polls the run record. Deleting a saved override
can reveal the bundled recipe again.

## Deep research with adversarial review

The bundled `deep-research` recipe uses this sequence:

```text
research ∥ dissent → gap_fill → antagonist ∥ verify → synthesize
```

It gathers opposing sources, fills gaps, challenges and verifies claims, then
writes a report addressing counterpoints and uncertainty. Run it with
`/deep-research <question>` or
`run_workflow("deep-research", {"topic": "…", "depth": "deep"})`.
It uses more subagent calls than a single researcher; reserve it for questions
that need that review. See [ADR 0011](/adr/0011-deep-research-workflow) for its design.

## The agent can author them (closed loop)

Ask “save that process as a workflow called competitor-scan.” The agent uses
`save_workflow(name, description, steps, inputs?, output?)` to validate and save
a recipe. Saving the same name replaces its writable copy. Review the recipe
and add human gates where a step needs an operator decision.

## Related

- [Configure subagents](/guides/subagents)
- [Schedule future work](/guides/scheduler)
- [Workflow design](/adr/0002-reusable-subagent-workflows)
