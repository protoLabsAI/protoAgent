- **The starter-tools reference is a usable index of the core tool set again.** The page
  opened with a wall of counted bullets ("four keyless…", "five memory…") that named 14 of
  the 39 tools `get_all_tools()` actually binds, gave detail sections for a different 14,
  and still documented `daily_log` — removed from core long ago. Now: a table of **every**
  core tool grouped by what turns it on, a "why a tool isn't bound" section covering the six
  real causes (plugin not enabled · backend absent · **goal/watch flag on but no plugin
  verifier registered** · `tools.disabled`/`tools.hidden` · subagent allowlist · deferred
  disclosure), a map of the five places the agent's tools come from (core · plugins ·
  `task`/`task_batch` · the filesystem fence · MCP), and a reference entry for all 39 —
  including the previously undocumented `knowledge_ingest`, `forget_memory`, `load_skill`,
  `show_component`, `onboard_project`, `list_verifiers`, the tasks, goal, watch and curation
  groups, and `search_tools`. Corrected along the way: `memory_recall`'s `domain` argument,
  `schedule_task`'s `timezone` argument, `fetch_url`'s SSRF/egress behavior, the GitHub tools
  (a separately installed plugin, not in-tree), and "add your own" no longer walks you through
  editing `get_all_tools()` and then tells you not to. The same stale summary in `README.md`
  and the first-agent tutorial was fixed to match.
