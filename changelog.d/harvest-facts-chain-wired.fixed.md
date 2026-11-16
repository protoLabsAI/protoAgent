- **Harvest and Facts model chains set in Settings now take effect at runtime.** The
  session-end harvest and fact-extraction passes resolved an aux-backed role that read
  nothing, so `memory.harvest_model` / `memory.facts_model` — surfaced in Settings › Model
  roles — changed nothing (ADR 0133). The `harvest` role now reads `memory.harvest_model`
  (blank inherits aux) and `facts` reads `memory.facts_model` (blank inherits harvest), and
  `GET /api/config/model-roles` lists each pass once instead of alongside a dead
  `memory_harvest` / `memory_facts` duplicate.
