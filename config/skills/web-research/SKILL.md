---
name: web-research
description: >-
  Use this whenever the user asks you to research a topic, find the current
  state of something, compare options, or gather background from the web —
  e.g. "what's the latest on X", "find the best approach to Y", "compare
  these three tools". Drives a scope → gather → gap-check → synthesize loop.
tools: [web_search, fetch_url, memory_recall, memory_ingest, current_time]
---

# Web Research

A disciplined pipeline for turning an open question into a tight, sourced answer.

## Scale to the ask
A fact lookup is one angle, one pass. "Compare X" is 3-5 dimensions.
"Comprehensive analysis of X" is 5-8 dimensions and more rounds. Don't
over-research a lookup or under-serve a survey.

## 1. Scope
Break the question into a few **orthogonal dimensions** — focused sub-topics
that together cover it (e.g. "Rust vs Go" → perf · memory · concurrency ·
ecosystem · adoption). A narrow question is one dimension; don't invent angles.
When dimensions are independent, fan them out as parallel `task` calls (or one
`task_batch`) instead of researching them serially.

## 2. Gather (per dimension)
- **Reuse first** — `memory_recall` what's already known; don't re-derive it.
- **Search wide, then deep** — `web_search` the dimension; for technical or
  contested topics run a second angle (parent topic, or community/code sources
  like Reddit/HN/GitHub/SO) so you don't trust one lens. Listicles are leads,
  not authority; prefer primary + recent.
- **Read selectively** — `fetch_url` the 2-4 best hits per dimension; read
  deeply. Keep a **numbered source list** + a one-line key finding per
  dimension as you go.

## 3. Gap-check (conservative)
Ask: does this answer the *original* question? Flag only 1-3 genuine gaps (not
tangents), research them as new dimensions, repeat — up to ~3 rounds. Don't
rewrite the question or chase saturation.

## 4. Synthesize
Bottom line first; `##` headings for multi-dimension answers. **Every material
claim carries a citation** to your numbered sources, inline as `[1]` (or
`[1][3]` where evidence converges). Cite both sides of a real disagreement and
say which is better-supported; flag uncertainty. List sources at the end. End
with `Confidence: high | medium | low`; for deep research add 3-5 "Related
topics".

## 5. Persist
For substantial research, `memory_ingest` one concise durable finding (takeaway
+ key sources) so the knowledge base compounds — not raw dumps, and not for
quick lookups.

Keep it tight: the answer first, the process never. Let the citations carry the
evidence.
