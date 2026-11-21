---
name: web-research
description: >-
  Use this whenever the user asks you to research a topic, find the current
  state of something, compare options, or gather background from the web —
  e.g. "what's the latest on X", "find the best approach to Y", "compare
  these three tools". Real research is handed to a helper as a long-running
  background task, so the chat stays free and the sourced result is reported
  when it arrives; a quick one-fact lookup is answered directly.
tools: [task, task_batch, web_search, fetch_url, memory_search, knowledge_search, memory_save, current_time]
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
- **A research workflow fits** (e.g. a deep-research recipe that adds a
  critique pass) → delegate its execution with `task` and
  `run_in_background=true` to a registered helper whose available tools include
  `run_workflow`. Include the recipe name and inputs in its brief. If no such
  helper is available, use the background `researcher` path above with the
  same research scope and review requirements. Do not call `run_workflow` from
  the chat: it waits for the entire workflow and has no background option.

After the tool confirms that a background job started (not if dispatch was
refused):

1. **Tell the operator it has started** — one line: what is being researched
   and that you will report back.
2. **Carry on.** Keep working on anything else, or end your turn. Don't poll,
   don't re-check, and don't start a duplicate.
3. **Report when the result arrives.** Lead with the bottom line and keep the
   helper's citations and confidence. For a fan-out, wait for every angle and
   synthesize once.

## What to put in the brief
The helper starts with none of your context, so the brief must stand alone.
Describe the work in plain words and **don't name tools** in it: the helper
knows its own tools, and a brief that names one the helper doesn't have is
refused before it starts. Say:

- **The question and why it is being asked** — the decision or review it
  feeds, any constraints the operator gave, and how deep to go (a comparison is
  3-5 dimensions; "comprehensive" is 5-8 and more rounds).
- **Gather** — reuse first (what memory and the operator's ingested documents
  already hold), then search the web wide and deep: a second angle for
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
- **Persist** — for substantial research, save one concise durable finding to
  memory (takeaway + key sources), not a raw dump.

## Quick lookup: answer directly
`current_time` first if "latest" matters, then one `web_search` or one
`fetch_url`. Answer in a sentence or two with the source. Don't delegate it and
don't save it to memory.

Keep it tight: the answer first, the process never. Let the citations carry the
evidence.
