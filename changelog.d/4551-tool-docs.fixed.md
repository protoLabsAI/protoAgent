- **Four more tool descriptions get back the when-to-use guidance the #4551 trim removed (#4551).**
  `memory_ingest` (when the operator asks you to remember or note something), `memory_recall`
  (examples of preference and past-decision questions, and a pointer to `memory_list` for the
  most recent entries), `session_search` (an example lookup) and `browser_get_text` (use it to
  read page content). One trimmed line had already broken `memory_list` routing (#4672). This
  restores the same kind of routing cue on the other tools that lost theirs. All the other
  trimmed descriptions kept theirs.
