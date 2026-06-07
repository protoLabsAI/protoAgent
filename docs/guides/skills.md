# Skills (`SKILL.md`)

protoAgent loads **human-authored skills** in the [AgentSkills](https://agentskills.io/specification)
open `SKILL.md` format — the same portable format Claude Code, Hermes, and
OpenClaw use. A skill teaches the agent *how and when* to use its tools for a
recurring task. Relevant skills are retrieved and injected into the system
prompt at inference time (the `<learned_skills>` block), so the agent picks the
right approach without you re-explaining it each turn.

> The console surfaces the skill index under **Knowledge → Skills**. A skill
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

The markdown **body** is the skill's instructions — freeform; write whatever
helps the agent perform the task.

## Where skills live

Two roots, mirroring protoAgent's config bundle/live split:

- **Bundled (shipped, read-only):** `config/skills/<slug>/SKILL.md` — example
  skills that travel with the agent (and into the desktop sidecar).
- **Your skills (writable, drop-in):** `<config-dir>/skills/<slug>/SKILL.md`,
  where `<config-dir>` is `PROTOAGENT_CONFIG_DIR` (defaults to `config/`).
  Override the root with `skills.dir` in the config.

If a live skill and a bundled skill share a `name`, the live one wins.
Sub-folders are organizational only — the skill is named by its frontmatter.

Skills are (re)loaded into the FTS index on server start; drop a folder in and
restart (or trigger a config reload) to pick it up.

## Configuration

```yaml
skills:
  enabled: true            # default
  db_path: /sandbox/skills.db   # falls back to ~/.protoagent/skills.db
  top_k: 5                 # max skills injected per turn
  dir: ""                  # optional override for the writable skills root
```

`GET /api/runtime/status` reports `skills.count` so you can confirm how many
loaded.

## Notes

- Skills are *retrieved by relevance* (BM25 over name/description/body), so a
  precise, trigger-oriented `description` matters most.
- This is the human-authored half. protoAgent also captures **agent-authored**
  skills from successful `task()` runs (skill-v1 procedural memory); surfacing
  those alongside `SKILL.md` skills is a planned follow-up.
- OS/binary gating fields are parsed but not yet enforced.
