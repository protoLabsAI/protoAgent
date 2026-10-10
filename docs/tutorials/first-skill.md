# Write your first skill

A **skill** teaches the agent how and when to handle a recurring task. You drop in a
`SKILL.md` folder; the agent loads its instructions when it needs them — no code, no
re-explaining each turn. By the end of this tutorial you'll have a skill the agent uses
automatically *and* can run on demand as a `/slash` command.

> Prerequisite: a running agent — see [Set up your first agent](/tutorials/first-agent).
> For the full format, see the [Skills reference](/reference/skills); for the concepts, the
> [Skills guide](/guides/skills).

## 1. Create the skill

Make a folder under your instance skills root with a `SKILL.md` inside. Use
`protoagent config explain` to find that root; a normal default host install uses:

```
~/.protoagent/default/skills/tldr/SKILL.md
```

```markdown
---
name: tldr
description: >-
  Use whenever the user asks for a TL;DR, a summary, or "the short version" of a
  document, thread, or block of text.
tools: []
---

# TL;DR

1. Read the provided text closely.
2. Reply with exactly three bullet points — the most important takeaways, in plain language.
3. Keep each bullet to one sentence and include any uncertainty that changes the conclusion.
```

The agent sees the **description** in its available-skills index and uses it to
decide whether to load the full procedure. State when the skill is useful.

## 2. Load it

The agent indexes `SKILL.md` files at boot, so restart the server (or trigger a config
reload). Confirm it loaded:

```bash
curl -s localhost:7870/api/runtime/status | grep -o '"skills":[^}]*'
```

> Shortcut: the console's **Settings → Skills → New** authors a skill **live** — no restart.
> The file path above is the portable, fork-friendly way and shows the raw format.

## 3. Watch it fire

In chat, ask something that matches the description:

> *"Give me a TL;DR of this: \<paste a few paragraphs\>"*

Watch for a **`load_skill` tool-call card** naming `tldr`. The reply should follow
the three-bullet procedure. If it does not load, check that the skill appears in
**Settings → Skills**, reload the config, and retry with a request that clearly
matches the description. Automatic selection is the model's decision; `/tldr`
below lets you choose it explicitly.

## 4. Make it runnable on demand

Add two frontmatter keys so the skill becomes a `/slash` command too:

```markdown
---
name: tldr
description: >-
  Use whenever the user asks for a TL;DR or summary of some text.
user_facing: true
slash: tldr
---
```

Restart, then type `/tldr` in the composer — it appears in the slash menu. `/tldr <text>`
runs the procedure on the spot (it rewrites the turn with your skill's body as the
directive; [ADR 0052](/adr/0052-user-facing-skills-slash-commands)).

## What you learned

- The description helps the agent choose a skill; the body supplies the procedure.
- `load_skill` shows which procedure the agent read.
- `user_facing` skills can also run as `/slash` commands.

Next: the [Skills guide](/guides/skills) (tiers, the curator, `/distill` self-authoring)
and the [Skills reference](/reference/skills) (every field + the `skills:` config block).
