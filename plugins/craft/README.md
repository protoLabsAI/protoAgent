# Craft

Engineering rituals as **user-only slash commands**, plus a skill-authoring
delegate. Prompt-only: this plugin registers no tools, routes, surfaces, or
config — the skills are the product.

| Command | What it does |
|---------|--------------|
| `/grill` | Relentless one-question-at-a-time interview that sharpens a plan before anything is built. |
| `/standup` | Operational status report on everything the agent owns — tasks, goals, schedule, background work, PRs. |
| `/code-review` | Two-axis review of a diff — Standards vs Spec — in parallel subagents whose findings are never merged. |
| `/writing-skills` | The house discipline for authoring predictable SKILL.md skills. |

It also registers the **`skill_writer`** subagent: delegate to it ("write a
skill for X", "tighten this skill") and it returns a complete SKILL.md, its
placement, and the slash token to collision-check.

All four skills set `user_only: true` — they are withheld from the agent's
skill retrieval and reachable only by the operator typing the slash command,
so they cost no context until used.

First-party and enabled by default. Disable per instance with:

```yaml
plugins:
  disabled: [craft]
```

## Attribution

The grilling, two-axis code-review, and skill-authoring discipline are
adapted from [mattpocock/skills](https://github.com/mattpocock/skills)
(MIT License, © 2026 Matt Pocock).
