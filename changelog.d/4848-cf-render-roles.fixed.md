- **A compaction can no longer be fooled by forged speaker lines (#4848).** When the
  conversation was summarized, a tool result (or system text) that happened to contain a line
  beginning "OPERATOR:" could be read by the summarizer as something you had said. Those blocks
  are now indented so only your real messages and the agent's start a line with a speaker label.
  When one very long message is split across chunks, every later slice now keeps its speaker
  label ("OPERATOR (continued):"), with room reserved so no chunk overruns the summarizer's
  budget. And a message only counts as a previous compaction's notes when it carries the internal
  tag that marks it as one — an ordinary message that merely opens with "Here is a summary of the
  earlier conversation" is kept verbatim as your words, not mistaken for the agent's own notes.
