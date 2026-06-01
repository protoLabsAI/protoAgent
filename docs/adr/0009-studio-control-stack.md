# ADR 0009 — The Studio control stack (goals · workflows · subagents · skills)

- **Status:** Accepted (2026-06-01) — model locked; console IA reshape + the surfaced gaps are follow-up PRs
- **Date:** 2026-06-01
- **Deciders:** Josh Mabry; protoAgent maintainers
- **Tags:** architecture, console, ux, information-architecture, goals, workflows, skills, subagents
- **Supersedes / Superseded by:** —

> Accepted. The operator console's "Studio" surface grew four sibling tabs —
> **Goals, Workflows, Skills, Batch subagents** — that read as a pile of separate
> ideas. They aren't peers: they are **four altitudes of one control loop**.
> This ADR locks the model — *what each concept is, how they compose, and the
> decision rules that tell them apart* — and the target console IA that makes the
> relationship legible. **Model + naming only here**; the console reshape and the
> gaps it surfaces ship as follow-ups.

---

## 1. Context & Problem Statement

Studio presents Goals / Workflows / Skills / Batch-subagents as co-equal tabs.
Operators (and we) can't say crisply when to use which — workflow vs `task_batch`
vs goal all look like "automation," and "Skill" even collides with the A2A
agent-card `skills` field. The cause is an information-architecture error, not
missing features: **the tabs are stacked layers of a single execution loop,
shown side by side.**

A parallel map of each subsystem confirmed the spine:

- **One execution primitive.** `task`, `task_batch`, and every `run_workflow`
  step all bottom out in `_run_subagent` (`graph/agent.py:553-566`). Workflows
  are *saved + ordered* calls to it; batch is *fanned-out* calls.
- **Goals don't dispatch work.** The goal loop re-invokes whole agent turns
  until a verifier passes (`server.py`); it owns *until-when*, not *how*.
- **Skills don't run.** They're BM25-retrieved methodology injected as
  `<learned_skills>` by `KnowledgeMiddleware.before_model` into *any* turn —
  dispatched by nothing, influencing every layer.

## 2. Decision — the control stack

Adopt one model: a **bottom-up control stack** with four altitudes; skills are
an orthogonal **memory layer** beside it (not in it).

```
GOAL      ── autonomy:      decides WHEN to stop   (re-invokes the agent until a verifier passes)
  ▲ drives
WORKFLOW  ── orchestration: decides the ORDER      (a saved DAG of subagent steps)
  ▲ dispatches
SUBAGENT  ── execution:     does the WORK           (the one primitive; task = 1, task_batch = N parallel)
  · · · · · · · · · · · · · · · · · · · · · · · · · · · · · · · · · · ·
SKILL     ── memory:        teaches HOW             (retrieved methodology injected into ANY turn)
```

### One-line definitions (so they stop blurring)

- **Skill** *(memory)* — reusable methodology the model retrieves; never runs,
  only advises. Human-authored `SKILL.md` = pinned; agent-emitted `skill-v1` =
  curated/decaying. Operator-facing name: **Playbooks** (see §4).
- **Subagent / batch** *(execution)* — a scoped LLM worker that does one focused
  unit of work under a tool allowlist + turn cap. `task` runs one;
  `task_batch` fans out N independent ones in parallel. The atom everything
  above is built from.
- **Workflow** *(orchestration)* — a saved, named, parameterized DAG of subagent
  steps with templated I/O threading. Deterministic structure, no autonomy.
- **Goal** *(autonomy)* — a server-side closed loop that re-invokes the agent
  until a testable verifier (command/test/CI/data/LLM) passes, the budget runs
  out, or it's declared unachievable. Owns *when-to-stop*; defines no steps.

### How they compose

- **Goal → agent turns.** The goal loop re-runs the *agent graph* with
  continuation prompts; the agent inside may call `task`/`task_batch`/
  `run_workflow`, but the loop dispatches none of them directly — it just
  re-invokes and checks the verifier.
- **Workflow → subagents.** `run_workflow` resolves inputs, runs each step via
  `_run_subagent`; independent steps parallelize up to
  `subagent_max_concurrency`. Saved via `save_workflow`.
- **task_batch → subagents.** Flat fan-out: N independent subagents, no edges,
  results in input order. Same runner + cap as workflows; the differences are
  *no DAG* and *truncated output*.
- **Skill → context (all layers).** `KnowledgeMiddleware.before_model` injects
  top-k skills on lead + subagent turns. Subagents may *emit* new skills
  (`emit_skill=True`) — the one feedback edge Execution → Memory.

### Decision rules (resolve the overlaps)

| Question | Use | Because |
|---|---|---|
| `task_batch` vs workflow | no deps → **task_batch**; deps/threading or you'll re-run it → **workflow** | batch is ephemeral + flat; a workflow is saved + a DAG |
| `task` vs `run_workflow` | one-off mid-turn → **task**; named repeatable process → **run_workflow** | a workflow is "a `task_batch` with edges and a saved name" |
| goal vs workflow | know the *steps* → **workflow** (*how*); know the *finish condition* not the steps → **goal** (*until-when*) | workflow is deterministic; goal is an autonomous loop |

## 3. Target Studio IA (the reshape — follow-up PRs)

Four peer tabs → **three layered tabs + two relocations**:

- **Studio = Goals · Workflows · Run** — `Run` carries the existing Single/Batch
  toggle (batch is a *mode*, not a tab). Order Goals → Workflows → Run
  ("outcome → recipe → worker"), with a one-line subhead per tab.
- **Schedule leaves Studio → a Triggers/System grouping.** Cron is a *trigger*
  ("when"), orthogonal to work-types and parallel to the inbox/event-bus
  triggers (ADR 0003) — not a fourth kind of work.
- **Skills leaves Studio → a Knowledge surface, renamed "Playbooks"** (§4).
  They're memory, not execution.

## 4. The "Skills" rename → "Playbooks"

protoAgent's `skill-v1` (procedural memory in `skills.db`, retrieved + injected)
is unrelated to the A2A agent-card `skills` field (declarative capability
labels). Same word, different concepts. **Operator-facing surfaces rename to
"Playbooks"** (honest: retrieved methodology). Internals (`SKILL.md`,
`skills.db`, the `skill-v1` artifact, the `tools:` frontmatter) and the A2A card
field are unchanged — this is a UX/label decision, not a data migration.

## 5. Gaps this surfaced (follow-up PRs, not part of locking the model)

- **No Playbooks/Skills surface at all.** Operators are blind to `skills.db` —
  no browse (disk vs emitted), confidence/last-used, search, pin/delete, or
  curator-`audit.jsonl` read-back. The biggest blind spot.
- **No goal → workflow bridge.** A goal can't verify a *workflow's* output today
  (workflow slash-commands short-circuit the goal loop). "Run this recipe until
  the verifier passes" needs the loop to dispatch/observe a workflow run.
- **Batch is opaque.** `task_batch` renders one concatenated tool output;
  workflows already render per-step cards. Batch should too.

## 6. Consequences

**Positive** — operators get a legible mental model + decision rules; the IA
mirrors the architecture (one primitive, layered); the `skills`/`Playbooks`
collision is resolved without a data migration.

**Negative / costs** — the console reshape touches navigation (`App.tsx` Surface/
studioTab) + tests; relocating Schedule/Skills changes muscle memory; the
follow-up gaps (Playbooks UI, goal→workflow bridge, batch cards) are real work,
deliberately deferred.

## 7. Open questions (deferred decisions)

- How much **Playbooks UI** to build first (read-only browse vs pin/delete/tune)?
- Build the **goal → workflow bridge** ("run a recipe until verified")?
- Give **task_batch per-task cards** to match workflow per-step cards?
- Final tab **order/subheads** wording in the reshape PR.

## 8. Related

- [ADR 0001 — Extensibility & Plugins](/adr/0001-extensibility-and-plugin-architecture) — skills/`SKILL.md`.
- [ADR 0002 — Reusable Subagent Workflows](/adr/0002-reusable-subagent-workflows) — the orchestration layer.
- [ADR 0003 — Reactive Agent](/adr/0003-reactive-agent-activity-thread) — the Triggers grouping Schedule joins.
- [ADR 0005 — Tool Pollution](/adr/0005-tool-pollution-and-progressive-disclosure) — skills' `tools:` frontmatter.
- Code seams: `graph/agent.py` (`_run_subagent`, `run_workflow`), `graph/goals/`,
  `graph/workflows/`, `graph/skills/`, `graph/middleware/knowledge.py`,
  `apps/web/src/app/App.tsx` (Surface / studioTab).
