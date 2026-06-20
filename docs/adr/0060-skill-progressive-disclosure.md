# 0060 — Skills: progressive disclosure (always-on index + load on demand)

Status: **Accepted**

## Context

Learned skills (`SkillV1Artifact`s in the FTS5 `SkillsIndex`, ADR 0041/0052) were
fed to the agent by a **per-turn BM25 retrieval**: `KnowledgeMiddleware.before_model`
built a query from the last human message **plus ~2K chars of the agent's own recent
output**, ran `SkillsIndex.load_skills(query, k=skills_top_k)`, and injected the
top-k **full skill bodies** (`prompt_template`, truncated to a 2K-token budget) as a
`<learned_skills>` block — on **every** model call in the tool loop.

This misfired in practice. With only a handful of skills indexed, top-5-of-2 can't
exclude anything, and BM25 has no relevance floor — so a generic query ("what tools
do you have?") pulled in **every** skill, full body and all, every turn. Because the
query included the agent's recent output, a turn that merely *mentioned* releasing or
researching re-summoned the `release-notes` / `web-research` procedures and pinned
them in context for the rest of the conversation. The skills were loud, always-on,
and unrelated to the task — the opposite of what a skill library should be.

The wider ecosystem converged on a different model. Anthropic's **Agent Skills** use
three tiers of *progressive disclosure*: the skill's name + description is always in
context; the full `SKILL.md` body is read **only when the model activates it**; deeper
references load on demand. Cursor's rule types draw the same line between "always" and
"agent-requested". The lesson: **advertise cheaply, load deliberately** — don't guess
relevance per turn.

## Decision

Replace per-turn retrieval with **progressive disclosure**:

1. **Always-on index.** `before_model` injects an `<available_skills>` block listing
   the `{name, description}` (and `/slash` when user-facing) of up to `skills.top_k`
   discoverable skills, most-recently-used first, with a `+N more (call list_skills)`
   hint when truncated. It is **query-independent** — the same table of contents every
   turn — so there is no relevance guess to misfire, and no skill body in context until
   one is asked for. Backed by the new `SkillsIndex.skill_summaries(limit)` /
   `discoverable_count()`.

2. **Load on demand.** A new lead-agent tool **`load_skill(name)`** returns one skill's
   **full procedure** (`SkillsIndex.get_skill(name)`). The model calls it the moment it
   judges a listed skill fits the task — surfacing as an ordinary tool card. It is in
   the keyless base set and the deferral always-on set, so it's reachable without first
   searching for it.

3. **Delete the legacy path.** `SkillsIndex.load_skills` / `_build_match_query` /
   `SkillRecord`, and the middleware's `load_skills` / `_build_skills_query` /
   `_format_learned_skills` / `_announce_skills` are removed (greenfield, not dead
   code). The `skills_loaded` custom event and its end-to-end "skills loaded" chip
   (backend `SKILLS_MIME` DataPart → frontend `skillsFromParts` → `ChatSurface` /
   `PaletteChat`) are removed too — `load_skill` tool cards make skill use visible
   without a bespoke surface.

4. **Config.** `skills.top_k` is repurposed from "retrieval k" to "skills listed in the
   index" (relabelled in Settings). `skills.announce` (the chip toggle) and the unused
   `skills.max_tokens` knob are removed.

`user_only` skills remain withheld from the index (`skill_summaries`) but resolvable by
`get_skill`, so a `/slash` invocation still loads one. The **external-brain feed**
(`runtime/context.py`, the ACP operator-MCP bridge) lists the same index and exposes
`load_skill`, so an ACP-driven turn behaves like a native one.

## Consequences

- **Skills stop polluting context.** A turn carries a short name+summary list, not a
  pile of full procedures — and only the skills the model actually loads cost body
  tokens. The "release-notes/web-research are always loaded" failure is gone.
- **The model chooses.** Skill use becomes a deliberate `load_skill` call (visible as a
  tool card) instead of an implicit per-turn retrieval. This matches how the model
  already treats subagents, workflows, and deferred tools.
- **`skills.top_k` semantics changed.** It now bounds the *index*, not retrieval; the
  full library is always reachable via `list_skills` + `load_skill` regardless.
- **No relevance ranking in context.** The index is recency-ordered, not query-ranked —
  acceptable because the model does the selecting. If the library grows large enough
  that `top_k` truncation hides useful skills, `list_skills` is the escape hatch (and a
  future ranking/grouping pass can refine the index ordering without reviving per-turn
  body injection).
