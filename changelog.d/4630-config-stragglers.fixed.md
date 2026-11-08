- **The bundled `web-research` skill and the engineer SOUL preset use the new memory tool names (#4630).**
  After the ADR 0125 rename they still told the agent to call `memory_recall` and `memory_ingest`,
  which no longer exist. The skill now uses `memory_search`, `knowledge_search` and `memory_save`,
  the preset uses `memory_save`, and a test keeps the removed names out of bundled skills and presets.
