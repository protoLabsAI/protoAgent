# 0054 — `dream` & `distill`: scheduled self-curation subagents

- Status: Accepted
- Date: 2026-06-15
- Builds on: ADR 0020 (subagents via the `task` tool), ADR 0002/0011 (subagent
  roles), ADR 0021 (knowledge store + fact consolidation by id), ADR 0028/skill
  curator (confidence decay/prune), ADR 0052 (slash dispatch), ADR 0003/0004/0053
  (scheduler — one-shot + recurring jobs).
- Inspired by: [MiMo-Code](https://github.com/XiaomiMiMo/MiMo-Code)'s `dream` /
  `distill` commands, adapted to protoAgent's stores and native scheduler.

## Context

An agent that runs for weeks accumulates two kinds of debt that nothing
proactively pays down:

1. **Memory drift.** Long-term memory (the knowledge store) gathers facts, but
   they go stale, get superseded, and pile up as near-duplicates. The only
   existing consolidation is *reactive* — `conversation_harvest` runs when a
   session is deleted/TTL'd. Nothing periodically folds recent work forward into
   durable facts, and nothing prunes the cruft.
2. **Workflow debt.** The agent repeats the same multi-step manual workflow over
   and over without ever packaging it into a reusable skill. The skill loop today
   is *emit-on-task* (a subagent run can emit a skill) + the *curator*
   (decay/dedup/prune of emitted skills) — but there is no step that **mines**
   recent activity for "this keeps happening, make it a skill."

MiMo-Code (built on opencode) solves both with two system-spawned subagents,
`dream` and `distill`, that read a raw trajectory SQLite DB *via bash* and either
write memory files or author skill/agent/command assets. Two things make a direct
port wrong for protoAgent: (a) it hands the consolidation agent **raw bash + a
writable SQLite DB** (the audit's standout risk — "the consolidation pass rewrote
the trajectory database"), and (b) it **hardcodes an auto-run interval** precisely
because "mimocode has no built-in scheduler." protoAgent already has a scheduler
(ADR 0003/0053).

## Decision

Add `dream` and `distill` as first-class **subagents** (`SUBAGENT_REGISTRY`), each
running on a small set of **scoped, mostly read-only tools** — no shell, no raw
SQL. They are invocable as `/dream` / `/distill` (slash dispatch resolves any
registry subagent, ADR 0020) and, because a scheduled job fires a normal A2A turn
through the same streaming dispatch, **schedulable with the existing scheduler**
(`schedule_task "/dream"`). No new scheduling code — "schedule them as they want"
is just the scheduler we already have.

### `dream` — memory consolidation **and** pruning

The full two-way job, not just the additive half:

- **Consolidate (add):** fold durable, verified, not-already-known facts into
  memory with `memory_ingest`.
- **Prune (forget):** remove superseded / duplicate / stale facts with a new
  `forget_memory(chunk_id, reason)` tool — exactly the `delete_by_id` path the
  ADR 0021 fact-consolidator already uses to replace superseded facts. To make
  pruning targetable, `memory_list` now leads each row with its `#<id>`.

Tools: `recent_activity`, `memory_recall`, `memory_list`, `memory_ingest`,
`forget_memory`, `current_time`.

### `distill` — workflow → skill packaging, **hybrid output**

Mine recent activity for repeated manual workflows, inventory existing skills
first (`list_skills`), then:

- **Auto-create** only high-confidence, clearly-missing skills with `save_skill`.
- **Propose** everything thinner / sensitive / extend-an-existing as a bead
  (`beads_create`) for human review.
- **Skip** one-off or low-evidence work.

Because it runs unsupervised on a schedule, the bias is *propose over create*.

Tools: `recent_activity`, `memory_recall`, `list_skills`, `save_skill`,
`beads_create`, `current_time`.

### The new tools (scoped, in `tools/lg_tools.py`)

- **`recent_activity(limit, window_hours)`** — read-only digest of what the agent
  actually did: the Activity feed (`activity_log.recent`) + a telemetry rollup
  (`telemetry_store.summary`). This is protoAgent's "trajectory" surface. It is
  thinner than opencode's per-tool-call DB (telemetry records *counts*, not which
  tool with which args), but it needs **no raw DB / shell access** — the whole
  "rewrote the trajectory DB" risk class cannot occur.
- **`list_skills()`** — read-only inventory of the skill index (name · source ·
  confidence) so distill reuses/extends instead of duplicating.
- **`save_skill(name, description, body, tools)`** — **additive-only**: refuses if
  a skill of that name already exists (it never overwrites). Saved with
  `source="distilled"`, so it flows through the existing **curator** (decay/prune)
  — a mistaken capture self-cleans rather than accumulating.
- **`forget_memory(chunk_id, reason)`** — deletes exactly one knowledge chunk by
  id (no bulk/wildcard delete). dream's prune half.

All four read their stores from `STATE` at call time (the `set_goal` pattern), so
`get_all_tools` needs no signature change; they self-gate when a store is absent.

### Wiring fix

The out-of-graph subagent runner (`run_manual_subagent` — the slash / scheduled /
console path, distinct from the lead's in-graph `task` tool) built its tool set
with `get_all_tools(knowledge_store, scheduler)` only. That silently dropped
allowlisted tools like distill's `beads_create`. It now mirrors the lead's full
set (`inbox_store`/`beads_store` from `STATE`, `goal_enabled` from config). A test
asserts every name in each subagent's allowlist resolves against the full set, so
this degradation can't recur.

## Consequences

- **Self-improving without new infrastructure.** Memory stays sharp and repeated
  workflows become skills, on whatever cadence the user schedules — reusing the
  scheduler, the knowledge store, the skill index, and the curator.
- **Safe by construction.** No shell, no raw SQL; the only writes are additive
  skill creation, additive memory ingest, and a targeted one-id memory delete.
  distill biases toward proposing (beads) over auto-creating; both subagents are
  prompted to treat recalled/activity text as **data, not instructions** (prompt
  injection from recorded content).
- **Honest about the data gap.** `recent_activity` summarizes turns + telemetry,
  not a full per-tool trajectory. If finer mining is wanted later, a richer
  read-only activity surface can be added behind the same tool without touching
  the subagents.

## Alternatives considered

- **User-facing skills (`/dream` `/distill` as SKILL.md, ADR 0052).** Lightest,
  but runs the procedure on the *lead* with the lead's full toolset — no scoping,
  and no clean "save a skill" capability. Subagents give bounded tools + max_turns.
- **A new read-only activity tool with a full trajectory DB (opencode parity).**
  Rejected for now: protoAgent has no per-tool-call store, and adding one is a
  larger change than the value warrants. `recent_activity` over the existing
  Activity feed + telemetry is the pragmatic surface.
- **Auto-run on a hardcoded interval (opencode's model).** Unnecessary — the
  scheduler already does cadence; the user schedules `/dream` / `/distill`
  whenever they want.
