# ADR 0020 — Console IA: run from Chat, manage from surfaces

- **Status:** Accepted (2026-06-04) — refines the console IA of ADR 0009; implementation in a 4-PR sequence
- **Date:** 2026-06-04
- **Deciders:** Josh Mabry; protoAgent maintainers
- **Tags:** console, ux, information-architecture, chat, workflows, subagents, knowledge, settings
- **Supersedes / Superseded by:** Refines the **console IA** of [ADR 0009](./0009-studio-control-stack.md) (the control-stack *model* stands; the *surfaces* change).

> ADR 0009 locked the control-stack **model** — goal → workflow → subagent, with
> skills as an orthogonal memory layer — and explicitly left the console reshape
> as a follow-up. That reshape shipped (Studio / Activity-as-triggers /
> Skills→Knowledge), and using it surfaced four IA seams. This ADR resolves them
> with one principle: **execution is a chat gesture, not a surface.** You *run*
> from Chat (slash commands); the rail surfaces *author and inspect*.

---

## 1. Context & Problem statement

The shipped console put a **Run** tab inside Studio ("launch a subagent, or a
batch, manually") next to **Workflows**, a **Knowledge** rail that renders *only*
Playbooks (no actual knowledge store), and a **Settings** surface that is a flat
~40-field / ~12-section vertical scroll. Four seams:

1. **Run is a redundant second way to dispatch work.** Workflows are already
   slash commands (`/<name>`, ADR 0002); `/goal` already short-circuits a turn.
   The Run tab is a forms-based re-implementation of "invoke a subagent" that the
   composer should own.
2. **"Knowledge" is mislabeled** — it shows Playbooks and nothing else, while the
   `knowledge/store.py` FTS5 base (findings, daily-log, harvested sessions) that
   actually feeds `<learned_skills>` is unbrowsable.
3. **Playbooks ⟷ Workflows feel overlapping** — both read as "structured
   know-how," with no surface cue for the difference.
4. **Settings has no hierarchy** — model config sits beside cache TTLs beside
   middleware toggles beside plugin integrations, in one scroll.

These are IA errors, not missing features. The control-stack model (ADR 0009) is
right; its *projection onto surfaces* conflated "run" with "manage."

## 2. Decision

### Principle: run from Chat, manage from surfaces

Every **runnable** thing — goal, workflow, subagent — is invoked as a **slash
command in Chat**, through the existing `_parse_slash_command` / `chatCommands()`
path that already serves `/goal` and `/<workflow>`. Rail surfaces stop being run
buttons; they author and inspect.

This follows directly from ADR 0009's spine: `task`, `task_batch`, and every
`run_workflow` step bottom out in the *same* `_run_subagent` primitive. If
there's one execution primitive, there should be one execution **gesture** — the
composer — not a primitive-per-tab.

### The four resolutions

| Seam | Resolution |
|---|---|
| Run redundancy (1) | **Subagents become slash commands** (`/researcher …`), registered into `chatCommands` alongside workflows. The **Run tab is removed; Studio collapses to Workflows only** (authoring/inspection of the DAGs you `/run`). |
| Mislabeled Knowledge (2) | **Knowledge becomes the memory layer made browsable**, with two sub-tabs: **Store** (searchable view over `knowledge/store.py`) and **Playbooks**. |
| Playbooks ⟷ Workflows (3) | Kept distinct by **execution model, made legible by placement**: a **Workflow** is an *active* authored DAG you invoke (lives in Studio, runs from Chat); a **Playbook** is *passive* methodology the middleware auto-retrieves and injects (lives in Knowledge, as memory). Workflows are programs; playbooks are memory. This is ADR 0009's "skills are an orthogonal memory layer beside the stack, not in it" — now reflected in the rail. |
| Settings sprawl (4) | **Regroup the flat sections into 5 categories** with in-surface sub-nav: **Agent** (Identity · Model · Routing), **Behavior** (Compaction · Caching · Goal mode · Tools), **Memory** (Knowledge recall · history/checkpoint · skills top-k), **Integrations** (Discord · Google · plugin-contributed, ADR 0019), **System** (Middleware · Runtime). A `section → category` map drives the sub-nav; plugin sections default to Integrations. |

### Target rail

| Rail | Was | Becomes |
|---|---|---|
| **Chat** | chat | chat **+ the run surface** (`/goal`, `/<workflow>`, `/<subagent>`) |
| **Activity** | thread · inbox · schedule | unchanged (parked — its own follow-up) |
| **Studio** | workflows · **run** | **workflows only** |
| **Knowledge** | playbooks | **Store** (searchable) · **Playbooks** |
| **System → Settings** | flat ~12 sections | **5 categories** with sub-nav |

## 3. Consequences

- **One mental model for "make it do something": type `/`.** Autocomplete already
  lists commands; subagents just join the list. No second forms UI to learn.
- **Surfaces get a clear job**: Studio authors workflows, Knowledge shows memory,
  Settings configures. None of them "run" anything.
- **Knowledge stops lying** — the store that drives retrieval is finally visible,
  which also aids debugging "why did it recall that?".
- **A REST knowledge-search endpoint** is needed for the console (the store has a
  query API + a `knowledge_search` tool, but no operator-facing route yet).
- **Subagent slash commands need argument ergonomics** — at minimum a free-text
  prompt; `key=value` parity with workflow commands where it makes sense.
- **Activity is deliberately untouched** here; it needs its own pass.

## 4. Implementation sequence (4 PRs)

1. **Subagents as slash commands** — register the subagent registry into
   `_parse_slash_command` / `chatCommands()` so `/<subagent> <prompt>`
   short-circuits a turn like a workflow does. Backend; unblocks #2.
2. **Drop Run; Studio = Workflows** — remove `RunPanel` + the `StudioTab`
   segmented control; Studio renders `WorkflowsSurface` directly. Frontend.
3. **Knowledge = Store + Playbooks** — add a REST knowledge-search endpoint + a
   searchable **Store** view; make Knowledge a two-tab surface (Store · Playbooks).
4. **Settings regroup** — add the `section → category` map + sub-nav; plugin
   sections land in Integrations.

Activity remains a separate, later effort.

## 5. Alternatives considered

- **Merge Playbooks into Workflows as one "Recipes" spectrum.** Rejected: it
  collapses two different execution models (auto-injected memory vs explicitly-run
  DAG) and would erase the skill *loop* (agents learning from their own runs),
  which only coheres as memory. Keeping them distinct-but-placed is clearer.
- **Keep Run as an "advanced" launcher.** Rejected: a second dispatch path is the
  exact confusion ADR 0009 set out to remove; the composer + autocomplete cover
  the same need without a parallel UI.
- **Settings as one scroll with anchor links.** Rejected: anchors don't reduce the
  cognitive load of co-located unrelated config; categories with sub-nav do.
