# Skills (`SKILL.md`)

protoAgent loads **human-authored skills** in the [AgentSkills](https://agentskills.io/specification)
open `SKILL.md` format — the same portable format Claude Code, Hermes, and
OpenClaw use. A skill teaches the agent *how and when* to use its tools for a
recurring task. Every available skill is listed in the agent's context as an
always-on `<available_skills>` index — just its name + one-line summary — and the
agent **loads a skill's full procedure on demand** with the `load_skill` tool the
moment it judges one fits the task ([progressive disclosure, ADR 0060](/adr/0060-skill-progressive-disclosure)).
A `load_skill` call shows in chat as an ordinary tool card, so you can see which
guidance shaped the turn. (This replaced the old per-turn relevance retrieval,
which injected full skill bodies on every model call.)

> The console surfaces the skill index under **Agent → Skills**. A skill
> **advises** (loaded guidance the model may adapt); it does **not** execute. For
> deterministic, run-the-same-steps orchestration across subagents, that's a
> [Workflow](/guides/workflows#skills-vs-workflows) — different tool, different altitude.

## Anatomy of a skill

A skill is a **folder containing a `SKILL.md`** file: YAML frontmatter followed
by a markdown body.

```markdown
---
name: web-research
description: >-
  Use this whenever the user asks you to research a topic, compare options, or
  gather background from the web. Be specific about WHEN to trigger.
tools: [web_search, fetch_url]   # optional, advisory
---

# Web Research

1. Plan briefly.
2. Search with web_search.
3. Read the best 2–4 sources with fetch_url.
4. Synthesize: bottom line first, claims with inline source URLs.
5. End with Confidence: high | medium | low.
```

### Frontmatter

| Field | Required | Notes |
|---|---|---|
| `name` | ✅ | Unique, lowercase-with-hyphens. |
| `description` | ✅ | ≤ 1024 chars. This is the **trigger signal** — write it "pushy": say plainly *when* the agent should reach for this skill, or it under-triggers. |
| `tools` (or `metadata.tools`) | — | Advisory list of tool names the skill uses. When the agent loads the skill (`load_skill`), these are surfaced to it as `Relevant tools:` so it knows which of its (already-bound) tools this skill relies on — a hint, not a gate. See [ADR 0005](/adr/0005-tool-pollution-and-progressive-disclosure). |
| `user_facing` | — | `true` makes the skill directly invokable as a `/<slash>` command in the chat composer (ADR 0052). Off by default. |
| `user_only` | — | `true` makes it an **operator-only** skill: a `/<slash>` command **withheld from the agent's `<available_skills>` index** (the agent never sees or loads it). Implies `user_facing`. Off by default. |
| `slash` | — | The trigger token for a `user_facing` skill (whitespace-free); blank → slug of `name`. e.g. `slash: web-research` → `/web-research`. |

The markdown **body** is the skill's instructions — freeform; write whatever
helps the agent perform the task.

## Run a skill on demand (`/slash`)

By default the agent decides when to `load_skill` one from the index. Mark a skill
`user_facing: true` (ADR 0052) and it *also* shows up as a `/<slash>` command in
the composer's slash menu. Invoking `/<slash> [input]` **rewrites the turn** to
inject the skill's procedure as a directive and runs it on the normal lead-agent
turn (full toolset, history intact) — it doesn't spawn a detached worker.
Precedence on a shared token: `goal` > workflow > subagent > skill. The shipped
`web-research` skill is user-facing as `/web-research`.

### Operator-only skills (`user_only`)

Some procedures should be **yours to invoke, not something the agent reaches for
on its own** — a deploy/rollback runbook, a destructive cleanup, a manual override,
a "do exactly this" command you'd rather the model not improvise around.

Set `user_only: true` (it implies `user_facing: true`):

```yaml
---
name: Deploy
description: Deploy + verify the service, then roll back on failure
user_only: true        # → /deploy works, but the agent never auto-loads it
slash: deploy
---
1. …procedure…
```

The skill is **excluded from the agent's `<available_skills>` index** — it never
appears in context, so the agent can't see or `load_skill` it — but it stays a
`/<slash>` command you can run on demand. In the Skills
panel a **`user-only`** badge marks which skills the agent can't load. Toggle
it in the editor with **"Hide from the agent — operator `/slash` command only."**

## Where skills live

Two roots, mirroring protoAgent's config bundle/live split:

- **Bundled (shipped, read-only):** `config/skills/<slug>/SKILL.md` — example
  skills that travel with the agent (and into the desktop sidecar).
- **Your skills (writable, drop-in):** `<config-dir>/skills/<slug>/SKILL.md`,
  where `<config-dir>` is `PROTOAGENT_CONFIG_DIR` (defaults to `config/`).
  Override the root with `skills.dir` in the config.

If a live skill and a bundled skill share a `name`, the live one wins.
Sub-folders are organizational only — the skill is named by its frontmatter.

Skills authored in the console (**Agent → Skills**) are indexed **live** — create,
edit, and delete take effect immediately, no restart. Skills you drop on disk by
hand (a new `SKILL.md` folder) are picked up on the next server start or config
reload, since there's no filesystem watch on the skill roots.

## Configuration

```yaml
skills:
  enabled: true            # default
  db_path: /sandbox/skills.db   # falls back to ~/.protoagent/skills.db
  top_k: 5                 # max skills listed in the always-on <available_skills> index
  dir: ""                  # optional override for the writable skills root
```

`top_k` caps the per-turn **index** (the rest stay reachable via `list_skills`,
and any one's full body loads on demand via `load_skill`) — not what's usable.
`GET /api/runtime/status` reports `skills.count` so you can confirm how many
loaded.

## Sharing skills across a fleet (the commons)

By default each agent's skill library is **private** (`scope: scoped`) — a fresh
agent never auto-publishes what it learns. To let a fleet pool skills, opt a
store into a shared **commons** ([ADR 0041](/adr/0041-workspaces-and-tiered-stores)):

```yaml
skills:
  scope: layered          # read commons ∪ private, write private, promote to share
commons:
  path: ~/.protoagent/commons   # host-level, shared by every agent that points here
```

- **`scope: shared`** — the whole skills library *is* the commons (read + write).
- **`scope: layered`** — "shared brain, private hands": the agent reads
  `commons ∪ private` (private shadows commons on a name clash) and writes to its
  **private** tier, so half-baked learned skills never pollute the fleet. You lift
  a proven one explicitly.

The commons is **host-level and un-scoped** — every agent pointing at the same
`commons.path` reads it, regardless of `instance.id`. Run two *isolated* fleets on
one host by giving each a distinct `commons.path`. The boot log names the active
tier and path (`[skills] tier=layered into …`).

Curate the skill library from the CLI:

```bash
python -m server skills ls                  # list both tiers + the commons path
python -m server skills promote <name>      # lift a private skill into the commons (upsert)
python -m server skills forget  <name>      # remove a skill from the commons
python -m server skills curate              # curate the PRIVATE tier (decay + dedupe + prune)
python -m server skills curate --tier commons   # curate the COMMONS — dedupe ONLY
```

`curate` runs the skill curator against **one concrete tier**. The **private** tier
gets the full pass (idle-decay → dedupe → prune-below-threshold). The shared
**commons** is curated + trusted, so it only **dedupes** — it never idle-decays (a
promoted runbook mustn't rot because the fleet was idle) and never auto-prunes
(removal is the explicit `forget`; pass `--prune` to also prune the commons). Add
`--dry-run` to preview.

## Notes

- The `description` is the skill's **summary in the index** — the only thing the
  agent sees before it decides to `load_skill` one, so write it precise and
  trigger-oriented (say plainly *when* to reach for the skill).
- The agent can also **author its own** skills: the `/distill` subagent looks back
  over recent work and turns a proven, repeated workflow into a new skill (its
  companion `/dream` consolidates memory). Both are schedulable (ADR 0054). The
  skill curator (`graph/skills/curator.py`, run via `skills curate`) decays + dedupes
  + prunes non-pinned **private** skills; disk skills (your `SKILL.md` files) are
  pinned; the **commons** is dedupe-only (no decay/auto-prune) and removed from via
  `skills forget`.
- Only `name` / `description` / `tools` / `user_facing` / `slash` are read; any other
  frontmatter keys are ignored. See the [Skills reference](/reference/skills) for the exact
  field + config schema.
