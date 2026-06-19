# Skills (`SKILL.md`)

protoAgent loads **human-authored skills** in the [AgentSkills](https://agentskills.io/specification)
open `SKILL.md` format — the same portable format Claude Code, Hermes, and
OpenClaw use. A skill teaches the agent *how and when* to use its tools for a
recurring task. Relevant skills are retrieved and injected into the system
prompt at inference time (the `<learned_skills>` block), so the agent picks the
right approach without you re-explaining it each turn. When that happens the
console shows a small **"Skills: …" chip** above the answer (hover a name for its
description) so you can see what guidance shaped the turn — set `skills.announce:
false` to silence it.

> The console surfaces the skill index under **Agent → Skills**. A skill
> **advises** (retrieved guidance the model may adapt); it does **not** execute. For
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
| `tools` (or `metadata.tools`) | — | Advisory list of tool names the skill uses. When the skill is retrieved for a turn, these are surfaced to the agent as `<relevant_tools>` so it knows which of its (already-bound) tools this skill relies on — a relevance hint, not a gate. See [ADR 0005](/adr/0005-tool-pollution-and-progressive-disclosure). |
| `user_facing` | — | `true` makes the skill directly invokable as a `/<slash>` command in the chat composer (ADR 0052). Off by default. |
| `slash` | — | The trigger token for a `user_facing` skill (whitespace-free); blank → slug of `name`. e.g. `slash: web-research` → `/web-research`. |

The markdown **body** is the skill's instructions — freeform; write whatever
helps the agent perform the task.

## Run a skill on demand (`/slash`)

By default a skill fires only when retrieval matches it. Mark a skill
`user_facing: true` (ADR 0052) and it *also* shows up as a `/<slash>` command in
the composer's slash menu. Invoking `/<slash> [input]` **rewrites the turn** to
inject the skill's procedure as a directive and runs it on the normal lead-agent
turn (full toolset, history intact) — it doesn't spawn a detached worker.
Precedence on a shared token: `goal` > workflow > subagent > skill. The shipped
`web-research` skill is user-facing as `/web-research`, and `release-notes` as
`/release-notes`.

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
  top_k: 5                 # max skills injected per turn
  announce: true           # show a "skills loaded" chip in chat (default)
  dir: ""                  # optional override for the writable skills root
```

`GET /api/runtime/status` reports `skills.count` so you can confirm how many
loaded.

## Notes

- Skills are *retrieved by relevance* (BM25 over name/description/body), so a
  precise, trigger-oriented `description` matters most.
- The agent can also **author its own** skills: the `/distill` subagent looks back
  over recent work and turns a proven, repeated workflow into a new skill (its
  companion `/dream` consolidates memory). Both are schedulable (ADR 0054). The
  skill curator (`graph/skills/curator.py`) decays and prunes non-pinned skills;
  disk skills (your `SKILL.md` files) are pinned.
- OS/binary gating fields are parsed but not yet enforced.
