# Write your first skill

A **skill** teaches the agent how and when to handle a recurring task. You drop in a
`SKILL.md` folder; the agent retrieves it by relevance and follows it — no code, no
re-explaining each turn. By the end of this tutorial you'll have a skill the agent uses
automatically *and* can run on demand as a `/slash` command.

> Prerequisite: a running agent — see [Spin up your first agent](/tutorials/first-agent).
> For the full format, see the [Skills reference](/reference/skills); for the concepts, the
> [Skills guide](/guides/skills).

## 1. Create the skill

Make a folder under your skills root with a `SKILL.md` inside. On the default instance:

```
config/skills/tldr/SKILL.md
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
3. End with a one-line bottom line prefixed **Bottom line:**.
```

The **`description` is the trigger** — it's what relevance retrieval matches against, so
say plainly *when* to reach for the skill. The **body** is the procedure the agent follows.

## 2. Load it

The agent indexes `SKILL.md` files at boot, so restart the server (or trigger a config
reload). Confirm it loaded:

```bash
curl -s localhost:7870/api/runtime/status | grep -o '"skills":[^}]*'
```

> Shortcut: the console's **Agent → Skills → New** authors a skill **live** — no restart.
> The file path above is the portable, fork-friendly way and shows the raw format.

## 3. Watch it fire

In chat, ask something that matches the description:

> *"Give me a TL;DR of this: \<paste a few paragraphs\>"*

Above the answer you'll see a **Skills: tldr** chip (a small book-icon row) — that's the
agent telling you it pulled your skill into the turn. The reply should come back as three
bullets + a bottom line, because that's the procedure you wrote. (No match in the message →
no chip; that's relevance retrieval working as intended.)

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

- A skill is just a `SKILL.md` (frontmatter + body) — the `description` triggers it, the
  body is the procedure.
- Retrieved skills surface as the **Skills** chip; `user_facing` skills also run as
  `/slash` commands.

Next: the [Skills guide](/guides/skills) (tiers, the curator, `/distill` self-authoring)
and the [Skills reference](/reference/skills) (every field + the `skills:` config block).
