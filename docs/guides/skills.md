# Skills (`SKILL.md`)

A `SKILL.md` teaches the agent when and how to use its tools for a recurring task.
Manage skills in **Settings → Skills**. The agent sees names and descriptions,
then loads a full procedure with `load_skill` when it needs it; that call appears
in chat as a tool card.

Skills are guidance the model can adapt. For a fixed sequence of subagent steps,
use a [workflow](/guides/workflows#skills-vs-workflows). To advertise a capability
to other agents, use an [A2A card entry](/guides/add-a-skill).

## Write one (the short path)

1. Open **Settings → Skills**, use the add button, and enter a name, a description
   of when to use it, and the procedure. Choose **Create skill**; it is live immediately.
2. Ask for a small task matching its description. Look for the `load_skill` card
   and check whether the result follows the procedure.
3. If selection is unreliable, improve the description or make the skill
   **Invokable as a /slash command** and choose it yourself.

Follow [Write your first skill](/tutorials/first-skill) for a complete example
and the file-based alternative. File edits need a configuration reload or restart.

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

The [Skills reference](/reference/skills#frontmatter-fields) owns the complete field
schema, including advisory tools and operator-only slash commands. The body
contains the procedure and its completion check.

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

Skills load from these roots:

- **Bundled:** `config/skills/<slug>/SKILL.md` in the installed app or source tree.
- **Live drop-ins:** `<instance_root>/config/skills/<slug>/SKILL.md`.
  `skills.dir` overrides this root.
- **Console-authored:** `<instance_root>/skills/<slug>/SKILL.md`.
- **Plugin skills:** directories registered by enabled plugins.

Use `protoagent config explain` to find the instance and config roots.

If a live skill and a bundled skill share a `name`, the live one wins.
Sub-folders are organizational only — the skill is named by its frontmatter.

Skills authored in the console (**Settings → Skills**) are indexed **live** — create,
edit, and delete take effect immediately, no restart. Skills you drop on disk by
hand (a new `SKILL.md` folder) are picked up on the next server start or config
reload, since there's no filesystem watch on the skill roots.

## Configuration

Skills are enabled by default. `skills.top_k` limits full descriptions in the
per-turn index; every discoverable skill keeps its name and slash identity.
Bodies load on demand. See [Skills configuration](/reference/skills#configuration-skills-block)
for fields, fallback defaults, and the bundled example's explicit override.

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
`commons.path` reads it, regardless of their instance root. Run two *isolated* fleets on
one host by giving each a distinct `commons.path`. The boot log names the active
tier and path (`[skills] tier=layered into …`).

Curate the skill library from a permanent package install with the CLI below.
For a source checkout, prefix commands with `uv run`; for a temporary package
run, use `uvx --from protolabs-agent protoagent skills …`.

```bash
protoagent skills ls                  # list both tiers + the commons path
protoagent skills promote <name>      # lift a private skill into the commons (upsert)
protoagent skills forget  <name>      # remove a skill from the commons
protoagent skills curate              # curate the PRIVATE tier (decay + dedupe + prune)
protoagent skills curate --tier commons   # curate the COMMONS — dedupe ONLY
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
- See the [Skills reference](/reference/skills) for supported frontmatter, including `user_only`.
