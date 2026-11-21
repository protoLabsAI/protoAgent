---
name: web-research
description: >-
  Use this whenever the user asks you to research a topic, find the current
  state of something, compare options, or gather background from the web —
  e.g. "what's the latest on X", "find the best approach to Y", "compare
  these three tools". Real research is handed to a helper as a long-running
  background task, so the chat stays free and the sourced result is reported
  when it arrives; a quick one-fact lookup is answered directly.
tools: [task, task_batch, run_workflow, web_search, fetch_url, memory_search, knowledge_search, memory_save, current_time]
user_facing: true
# `web-research`, not `research`: the deep-research workflow already owns
# /research and wins dispatch, which would shadow this skill (bd-2zh).
slash: web-research
---

# Web Research

Real research is **delegated as a long-running background task**. A quick
lookup you answer yourself. Decide which one this is before you search.

## Scale to the ask
The line is how much reading the answer needs:

- **Quick lookup — do it directly.** One fact, or one page: "what's the latest
  stable version of X", "when was Y released", "what does this page say". One
  search or one fetch settles it.
- **Real research — delegate it.** A briefing, a survey, background for a
  decision, "research it properly", or a comparison across several dimensions.
  Anything that needs several searches, several sources read, and a sourced
  write-up.

If you are unsure, it is real research: several searches run in the chat tie it
up, a background task does not.

## Real research: delegate, then carry on
Do **not** run the searches yourself in the chat. Hand the work to a helper:

- **One question** → `task` with `subagent_type="researcher"` and
  `run_in_background=true`.
- **Independent angles** (e.g. "Rust vs Go" → perf · memory · concurrency ·
  ecosystem · adoption) → one `task_batch` with `run_in_background=true`, one
  researcher per angle. Don't invent angles a narrow question doesn't have.

**The research workflow is the one exception, and only on request.** When the
operator asks for a research workflow by name (e.g. "run deep-research on X"),
call `run_workflow` with that recipe yourself. It cannot be handed to a helper
(helpers are never given `run_workflow`) and it has no background option: the
call holds the chat until the whole workflow ends. So say in one line, before
you call it, that the chat will be busy until it finishes, and report the
result when the call returns. Don't pick a workflow on your own for an
ordinary research request, and never as a way to keep the chat free: that is
what the background `researcher` is for.

After `task` / `task_batch` confirms that a background job started:

1. **Tell the operator it has started** — one line: what is being researched
   and that you will report back.
2. **Carry on.** Keep working on anything else, or end your turn. Don't poll,
   don't re-check, and don't start a duplicate.
3. **Report when the result arrives.** Lead with the bottom line and keep the
   helper's citations and confidence. For a fan-out, wait for every angle and
   synthesize once. For substantial research, save one concise durable finding
   to memory yourself (takeaway + key sources), not a raw dump.

**If the delegation is refused** (the tool returns an error, or there is no
`researcher` helper), don't retry the same call and don't go quiet. Tell the
operator in one line that the background run could not start and why, then do
the research yourself in the chat to the standard in the brief below, so they
still get a sourced answer.

## What to put in the brief
The helper starts with none of your context, so the brief must stand alone.
Describe the work in plain words and **don't name tools** in it: the helper
knows its own tools, and a brief that names one the helper doesn't have is
refused before it starts. Say:

- **The question and why it is being asked** — the decision or review it
  feeds, any constraints the operator gave, and how deep to go (a comparison is
  3-5 dimensions; "comprehensive" is 5-8 and more rounds).
- **What is already known** — paste in anything relevant you found in memory
  or the operator's ingested documents, so the helper builds on it rather than
  re-deriving it. Check these yourself first: the helper may not be able to.
- **Gather** — search the web wide and deep: a second angle for
  technical or contested topics (the parent topic, or community/code sources
  such as Reddit/HN/GitHub/SO). Listicles are leads, not authority; prefer
  primary and recent sources. Read the 2-4 best hits per dimension in full and
  keep a numbered source list.
- **Gap-check** — ask whether the findings answer the *original* question;
  chase only 1-3 genuine gaps, for up to ~3 rounds. No tangents, no rewriting
  the question.
- **Synthesize** — bottom line first, `##` headings for multi-dimension
  answers.
- **Cite** — every material claim carries an inline citation (`[1]`, or
  `[1][3]` where evidence converges), both sides of a real disagreement are
  cited with which is better supported, and the sources are listed at the end.
- **Confidence** — end with `Confidence: high | medium | low`; for deep
  research add 3-5 "Related topics".
## Quick lookup: answer directly
`current_time` first if "latest" matters, then one `web_search` or one
`fetch_url`. Answer in a sentence or two with the source. Don't delegate it and
don't save it to memory.

Keep it tight: the answer first, the process never. Let the citations carry the
evidence.
