- **The agent reaches for `memory_list` again on "what did I do today?"-style questions (#4551).**
  The tool-schema trim in #4551 dropped `memory_list`'s when-to-use line. After that the agent
  answered recent-log questions with `memory_recall` or `recent_activity` instead, and the
  `memory_list_intent` gate case failed every run: 0/9 at the trim commit and later, against
  4/4 passes just before it. One sentence is back, saying when to use it and pointing at
  `memory_recall` for topic search. It passes 3/3 again.
