# Craft — engineering slash commands

The rituals this repo runs on, packaged as things you *type*. Prompt-only: this
plugin registers no tools, routes, surfaces, or config — the skills are the
product, and they cost no context until you invoke one.

## Slash commands (you type these)

| Command | What it does |
|---------|--------------|
| `/grill` | Relentless one-question-at-a-time interview that sharpens a plan before anything is built. |
| `/standup` | Operational status report on everything the agent owns — tasks, goals, schedule, background work, PRs — ending in decisions that need your call. |
| `/code-review` | Adversarial review of a diff via the `code-review` workflow: four parallel finder angles → dedup → an evidence-checking verify pass → a findings report ([ADR 0077](../../docs/adr/0077-adversarial-code-review-workflow.md)). Falls back to a two-axis inline review when the workflow isn't available. |
| `/due-diligence` | Validates a technology or architecture choice — codebase map + external research in parallel, an antagonist pass, then a cited adopt/build/defer verdict. |
| `/writing-skills` | The house discipline for authoring SKILL.md skills that behave predictably. |

These five set `user_only: true` — they're withheld from the agent's skill
retrieval, so the slash is the only way in.

## What the agent reaches for on its own

- **`adr-authoring`** — the one agent-retrievable skill here. The house MADR
  shape, numbering, the index row, the docs-nav regeneration step, and the
  VitePress traps that fail the docs build. It exists precisely so
  *agent-authored* ADRs meet the bar without you asking.
- **`skill_writer`** subagent — delegate to it ("write a skill for X", "tighten
  this skill") and it returns a complete SKILL.md, where it belongs, and the
  slash token to collision-check. It never writes files itself.

## Enabling / disabling

First-party and enabled by default. Disable per instance with:

```yaml
plugins:
  disabled: [craft]
```

## Attribution

The grilling, code-review, and skill-authoring discipline are adapted from
[mattpocock/skills](https://github.com/mattpocock/skills) (MIT License,
© 2026 Matt Pocock).
