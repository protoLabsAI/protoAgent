# 0052 — User-facing skills (slash commands in chat)

- Status: Accepted
- Date: 2026-06-14
- Builds on: ADR 0020 (subagents-as-slash-commands), ADR 0041 (layered skills —
  commons ∪ private), ADR 0002 (workflows-as-slash-commands), and the skills
  retrieval-injection path (`KnowledgeMiddleware` + the FTS5 `skills_fts` index).

## Context

Skills (SKILL.md → `SkillV1Artifact` → FTS5 index) are today **implicit**: the
`KnowledgeMiddleware` retrieves the top-k relevant skills for a turn and injects
them as a `<learned_skills>` block, so a skill only fires when the model's query
happens to match. There is no way for the **user** to say "run *this* procedure
now" — the way they already can with `/<workflow>` (ADR 0002) and `/<subagent>`
(ADR 0020).

That's a real gap: a well-authored skill is a reusable, named procedure the user
wants on demand (`/research uv vs poetry`, `/release-notes`), not a thing that
only surfaces when the retrieval lottery lands. The composer already renders a
generic slash menu fed by `/api/chat/commands`, so the surface exists — what's
missing is (a) a way to mark a skill as directly invokable and (b) a dispatch
that runs it.

## Decision

Add an opt-in **`user_facing`** flag (+ optional **`slash`** token) to a skill.
A user-facing skill is offered as a `/<slash>` command in the composer and, when
invoked, **rewrites the turn** to inject the skill's procedure as a directive and
**falls through to the normal lead-agent turn** — it does *not* spawn a worker or
short-circuit.

### Why fall-through, not a worker (the key choice)

`/<workflow>` and `/<subagent>` short-circuit the turn and run detached work,
formatting the result as the reply. A skill is different: it's a *procedure for
the lead agent to follow*, with the agent's full toolset, on the current chat
thread, with history intact. So `/<skill> <args>` simply rewrites `message` to:

```
[Running the '<name>' skill]

Follow this procedure:

<prompt_template>

Input: <args>          # omitted when no args
```

…and lets the existing turn run. This reuses `_run_turn_stream` verbatim → every
streaming / HITL / goal / tool-card invariant holds, and **ChatSurface.tsx needs
zero change** (the slash menu is already generic over `/api/chat/commands`).

### Layers

1. **Schema** (`graph/extensions/skills.py`) — `SkillV1Artifact` gains
   `user_facing: bool = False` + `slash: str = ""` and a `slash_token()` helper
   (explicit `slash`, else a slug of `name`: lowercased, non-alphanumerics →
   hyphens). Off by default — only deliberately-authored skills become invokable.
2. **Loader** (`graph/skills/loader.py`) — `parse_skill_md` reads the
   `user_facing` (truthy spellings) + `slash` frontmatter keys.
3. **Index** (`graph/skills/index.py`) — schema **v3 → v4** (two UNINDEXED columns
   `user_facing`/`slash`, auto-migrated by the existing backup-and-rebuild on
   version bump); `add_skill` writes them (slash defaults to the slug), `all_skills`
   reads them, and a new **`user_facing_skills()`** reader returns only the flagged
   rows. Reads tolerate pre-v4 rows.
4. **Layered** (`graph/skills/layered.py`) — `user_facing_skills()` unions both
   tiers, de-duped by slash token (private wins); `promote()` carries the flags.
5. **Advertise** (`operator_api/console_handlers.py`) — `_operator_chat_commands()`
   appends each user-facing skill as `{name: <token>, description, usage:
   "/<token> [input]"}`, skipping `goal` and any token a workflow/subagent already
   owns (those win in dispatch).
6. **Dispatch** (`server/chat.py`) — `_parse_skill_command` matches a `/<token>`
   message against `user_facing_skills()` (deferring to workflow/subagent of the
   same token), and `_skill_directive` builds the injected text. Wired into **both**
   the streaming (`_chat_langgraph_stream`) and non-streaming (`_chat_langgraph`)
   paths as a rewrite-and-fall-through, after the workflow/subagent blocks.

### Precedence

`goal` > workflow > subagent > skill, on a shared token. The skill dispatch and
the command advertiser both defer, so a name a worker owns never gets shadowed.

## Consequences

- **A skill becomes a first-class composer gesture** with no new surface and no
  frontend change — the slash menu picks it up from `/api/chat/commands`.
- **Opt-in and safe.** `user_facing` defaults off; an un-flagged skill behaves
  exactly as before (implicit retrieval-injection only). The two new columns are
  UNINDEXED metadata — no effect on FTS ranking or the retrieval path.
- **One migration.** The schema bump rebuilds the index from disk + persisted
  emitted skills on next boot (the established v2/v3 path); no data loss.
- **`web-research` ships user-facing as `/research`**, and a new `release-notes`
  skill (`/release-notes`) is the worked example.
- The injected directive is plain text in the user-turn slot, so the agent treats
  the procedure as guidance, not a system override — it can still reason about
  whether a step applies (the same posture as the `<learned_skills>` block).

## References

- ADR 0020 (`/<subagent>`) and ADR 0002 (`/<workflow>`) — the dispatch precedent
  this mirrors (`_parse_subagent_command` / `_parse_workflow_command`).
- `graph/skills/{loader,index,layered}.py`, `graph/extensions/skills.py`,
  `operator_api/console_handlers.py` (`_operator_chat_commands`), `server/chat.py`
  (`_parse_skill_command` / `_skill_directive`).
- `config/skills/web-research/SKILL.md` (now `/research`) and
  `config/skills/release-notes/SKILL.md` (the new worked example).
