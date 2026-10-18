# Write your first skill

A **skill** teaches the agent how and when to handle a recurring task. Create it in Settings or use a
`SKILL.md` folder; the agent loads its instructions when it needs them — no code, no
re-explaining each turn. By the end of this tutorial you'll have a skill the agent uses
automatically *and* can run on demand as a `/slash` command.

> Prerequisite: a running agent — see [Set up your first agent](/tutorials/first-agent).
> For the full format, see the [Skills reference](/reference/skills); for the concepts, the
> [Skills guide](/guides/skills).

## 1. Create the skill

Open **Settings → Skills** and use the add button to open **New skill**:

- **Name:** `tldr`
- **Description:** “Use whenever the user asks for a TL;DR, a summary, or the short version of a document, thread, or block of text.”
- **Procedure:** read the provided text; reply with exactly three one-sentence bullets; include uncertainty that changes the conclusion.

Choose **Create skill**. It is available immediately.

::: details File alternative
Create `<instance_root>/skills/tldr/SKILL.md`. Find the root with the [configuration command for your install](/guides/backup-and-restore#find-your-data).
Use this file:

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

:::

## 2. Load it

Confirm `tldr` appears in **Settings → Skills**. App-created skills are already
live. For a file created by hand, reload configuration or restart the server,
then check the list. If it is missing, check its path and required frontmatter.

## 3. Watch it fire

In chat, ask something that matches the description:

> *"Give me a TL;DR of this: \<paste a few paragraphs\>"*

Watch for a **`load_skill` tool-call card** naming `tldr`. The reply should follow
the three-bullet procedure. If it does not load, check that the skill appears in
**Settings → Skills**, reload the config, and retry with a request that clearly
matches the description. Automatic selection is the model's decision; `/tldr`
below lets you choose it explicitly.

## 4. Make it runnable on demand

Edit the skill, enable **Invokable as a /slash command**, enter `tldr` as the
slash token, and save. Type `/tldr` in the composer to find it in the slash menu.
Run `/tldr <text>` to invoke the procedure explicitly.

For the file alternative, add `user_facing: true` and `slash: tldr` to its
frontmatter, then reload configuration. This runs the skill in the normal chat
turn; it does not create a separate worker.

## What you learned

- The description helps the agent choose a skill; the body supplies the procedure.
- `load_skill` shows which procedure the agent read.
- `user_facing` skills can also run as `/slash` commands.

Next: the [Skills guide](/guides/skills) (tiers, the curator, `/distill` self-authoring)
and the [Skills reference](/reference/skills) (every field + the `skills:` config block).
