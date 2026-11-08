- **Your messages now survive compaction word for word (#4848).**
  When a chat is compacted, the agent used to keep only its own summary of what you said, so a
  weak summary could still lose or blur an instruction like "merge on PASS, stop on a WARN or
  FAIL". Automatic compaction, `/compact` and the overflow safety valve now end the compacted
  context with every message you typed, exactly as written and with the time you sent it, and
  carry that list forward through later compactions. It is capped at about 20K tokens: the newest
  messages are kept and older ones are replaced by a note saying how many there were and where to
  read them in the chat archive. Scheduler wakes, goal-loop prompts and other automated messages
  are marked as system-generated and are never presented as yours.
