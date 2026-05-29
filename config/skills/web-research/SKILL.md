---
name: web-research
description: >-
  Use this whenever the user asks you to research a topic, find the current
  state of something, compare options, or gather background from the web —
  e.g. "what's the latest on X", "find the best approach to Y", "compare
  these three tools". Drives a plan → search → read → synthesize → cite loop.
tools: [web_search, fetch_url]
---

# Web Research

A disciplined loop for turning an open question into a tight, sourced answer.

1. **Plan briefly.** What does the question actually need? What angles are
   worth covering? Note them before searching.
2. **Search.** Use `web_search` to find candidate sources. Prefer primary
   sources and recent material; treat listicles as leads, not authority.
3. **Read selectively.** `fetch_url` the 2–4 most promising sources. Read
   deeply rather than skimming ten shallow hits.
4. **Synthesize.** Lead with the bottom line, then 2–4 specific claims, each
   with its source URL inline. Surface disagreement between sources when it
   matters.
5. **Rate confidence.** End with `Confidence: high | medium | low` based on
   source quality and consensus.

Keep it tight: the answer first, the process never. Don't pad with "I searched
for…"; deliver the conclusion and let the citations carry the evidence.
