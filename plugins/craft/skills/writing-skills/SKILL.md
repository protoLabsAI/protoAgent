---
name: writing-skills
description: >-
  /writing-skills — the house discipline for authoring SKILL.md skills that
  behave predictably. Loads the rules; the skill_writer subagent applies them.
user_only: true
slash: writing-skills
---

# Writing Skills

A skill exists to wrangle determinism out of a stochastic system.
**Predictability** — the agent taking the same *process* every run, not
producing the same output — is the root virtue; every rule below serves it.

A protoAgent skill is a folder holding one `SKILL.md`: YAML frontmatter
(`name` + `description` required, description ≤ 1024 chars) over a markdown
body that becomes the agent's working instructions when the skill loads.
Deep docs: `docs/guides/skills.md`, `docs/guides/add-a-skill.md` (ADR 0052,
ADR 0060).

## Invocation classes — choose deliberately

- **Retrievable (default)** — indexed in the always-on `<available_skills>`
  list; the agent loads it with `load_skill`. Its description is scanned
  every turn, so the description is a real cost: write it as **triggers, not
  identity** — "Use when the user …, mentions …", one trigger per genuinely
  distinct branch, synonyms collapsed.
- **`user_facing: true` + `slash: <token>`** — additionally invokable as
  `/<token>` in chat.
- **`user_only: true`** — withheld from agent retrieval entirely; the slash
  is the only way in. Zero context cost; the *user* becomes the index that
  must remember it exists. The description turns human-facing: one plain
  sentence for the palette. Use this for rituals only ever fired by hand.

**Token collision check, every time:** slash precedence is goal > plugin
command > workflow > subagent > skill — a same-token workflow or subagent
silently shadows the skill (this is why `web-research` isn't `/research`).

## Body discipline

- **Steps end on a checkable completion criterion** — the agent must be able
  to tell done from not-done ("every modified surface accounted for", not
  "produce a list"). A fuzzy criterion invites premature completion.
- **Prefer a leading word** — a compact concept the model already holds
  (*tracer bullet*, *tight*, *red*) — over a restated triad; it anchors a
  region of behavior in one token, in the body (execution) and the
  description (invocation) alike.
- **Reference the docs, don't inline them.** Material only some runs need
  belongs in repo docs the body points at; the body carries what every run
  needs.

## Failure modes (diagnose with these)

- **Premature completion** — a step ends before it's done; sharpen the
  criterion first, split the sequence only if that fails.
- **Duplication** — one meaning in two places; costs tokens and maintenance.
- **Sediment** — stale layers that settle because adding feels safe and
  removing feels risky; the default fate of an unpruned skill.
- **Sprawl** — too long even when every line is live; push reference out.
- **No-op** — a line the model already obeys ("be thorough"); delete the
  sentence, don't trim it — or replace the weak word with a stronger one
  (*relentless*).

## Placement & workflow

Operator-authored skills live at `~/.protoagent/skills/<slug>/SKILL.md`
(create/edit via the console Skills surface — it round-trips the same
loader). Plugin-bundled skills live in the plugin's `skills/` dir; repo
examples in `config/skills/`. To draft or tighten one, delegate to the
**skill_writer** subagent — it returns the complete SKILL.md, its placement,
and the token to collision-check.

*(Discipline adapted from mattpocock/skills `writing-great-skills`, MIT.)*
