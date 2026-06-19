---
name: answering-protoagent-docs
description: >-
  Use whenever the user asks how protoAgent works, or about a specific feature,
  configuration option, tool, plugin, API endpoint, or design decision (ADR) — anything
  answerable from the project's own documentation. Reach for this before guessing.
tools: [docs_search, docs_read]
---

# Answering questions from the protoAgent docs

protoAgent ships its full documentation, searchable via the `docs` plugin. When a question
is about how protoAgent itself works, don't answer from memory — look it up:

1. Call **`docs_search`** with the user's question (or its key terms). It returns the best
   pages as `[section] Title — path` lines.
2. Read the **1–3 most relevant** hits with **`docs_read(path)`**.
3. Answer from what you actually read — concrete and specific. **Cite the doc path(s)** you
   used (e.g. "see `guides/skills.md`") so the operator can follow up.
4. If `docs_search` returns nothing useful, say so plainly rather than inventing an answer.
