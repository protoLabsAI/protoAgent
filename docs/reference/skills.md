# Skills (`SKILL.md`) reference

Exact shapes for the skill format and store. For *how and when* to use skills, see the
[Skills guide](/guides/skills); for the design, [Memory & the knowledge store](/explanation/memory-and-knowledge).

A skill is a folder containing a `SKILL.md` — YAML frontmatter, then a markdown body.

## Frontmatter fields

These are the only keys the loader (`graph/skills/loader.py`) reads. Any other frontmatter
key is **ignored** (no OS/binary/license gating is parsed or enforced).

| Field | Type | Required | Notes |
|---|---|---|---|
| `name` | string | ✅ | Unique. Lowercase-with-hyphens by convention. A live skill with the same `name` overrides a bundled one. |
| `description` | string | ✅ | The retrieval **trigger signal** (BM25 over name/description/body). Truncated at **1024 chars**. Write it "pushy" — say plainly *when* to use the skill. |
| `tools` (or `metadata.tools`) | list[string] | — | Advisory tool names the skill relies on. Surfaced to the model as `<relevant_tools>` when retrieved — a hint, not a gate ([ADR 0005](/adr/0005-tool-pollution-and-progressive-disclosure)). |
| `user_facing` | bool | — | `true` → the skill is invokable as a `/<slash>` chat command ([ADR 0052](/adr/0052-user-facing-skills-slash-commands)). Default `false`. Truthy spellings: `true` / `1` / `yes` / `on`. |
| `user_only` | bool | — | `true` → an **operator-only** skill: it's a `/<slash>` command, but it is **withheld from the agent's retrieval** (`load_skills`) — the agent never auto-loads it into `<learned_skills>`. **Implies `user_facing`.** Default `false`. Use it for procedures you want to run on demand without the agent reaching for them on its own. |
| `slash` | string | — | The `/<token>` trigger for a `user_facing` skill (whitespace-free). Blank → slugified `name` (lowercased, non-alphanumerics → hyphens). |

The markdown **body** is the skill's procedure — freeform instructions, used verbatim as
the injected guidance (and as the directive when invoked via `/slash`).

```markdown
---
name: web-research
description: >-
  Use whenever the user asks you to research a topic, compare options, or gather
  background from the web.
tools: [web_search, fetch_url]
user_facing: true
slash: web-research
---

# Web Research
1. Plan briefly.
2. Search with web_search …
```

## Where skills load from

Roots, later wins on duplicate `name`:

| Root | Source tag | Writable |
|---|---|---|
| `<repo>/config/skills/<slug>/SKILL.md` (bundled examples) | `disk` | read-only |
| `<config-dir>/skills/<slug>/SKILL.md` (`skills.dir` override) | `disk` | ✅ |
| plugin-bundled skill dirs | `disk` | via the plugin |
| operator/console-authored (`user_skills_dir()`) | `disk` | ✅ (live CRUD) |

Sub-folders are organizational only — a skill is named by its frontmatter, not its path.

## The index & source tags

Skills live in a SQLite/FTS5 index (`skills.db`). Each row carries a `source`:

| `source` | Produced by | Curated? |
|---|---|---|
| `disk` | `SKILL.md` files + console CRUD (re-seeded each boot) | **pinned** — never decayed/pruned |
| `distilled` | the `/distill` subagent (`save_skill`) | yes (curator) |
| `promoted` | `python -m server skills promote <name>` → the commons (layered tier) | yes (curator) |

The curator (`python -m graph.skills.curator`) decays + prunes non-`disk` skills; see the
[Skills guide](/guides/skills).

## Configuration (`skills:` block)

| Key | Default | Meaning |
|---|---|---|
| `enabled` | `true` | Load + retrieve skills at all |
| `db_path` | `/sandbox/skills.db` | Index location (→ `~/.protoagent/skills.db` fallback) |
| `top_k` | `5` | Max skills injected into the prompt per turn |
| `announce` | `true` | Show the "Skills" chip (auto-retrieved skills) in chat |
| `dir` | `""` | Override the writable skills root |
| `scope` | `""` (→ `scoped`) | Tier: `scoped` (private) · `shared` (one commons) · `layered` (read commons ∪ private, write private) ([ADR 0041](/adr/0041-workspaces-and-tiered-stores)) |
| `shared` | `false` | Back-compat boolean — `true` → `scope: shared` when `scope` is blank |

The shared-tier commons base dir is `commons.path` (blank → `~/.protoagent/commons`).
`GET /api/runtime/status` reports `skills.count`.
