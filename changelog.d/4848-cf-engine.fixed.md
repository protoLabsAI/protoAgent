- **Compaction no longer forgets an approval you gave earlier in the chat (#4848).**
  When a long chat was compacted, the summarizer only saw its most recent part, so an
  instruction like "once A and B merge, cut the release; stop on a WARN or FAIL" could vanish
  behind one long tool result, and the previous compaction's summary was dropped too.
  Automatic compaction, `/compact` and the overflow safety valve now share one engine that reads
  the whole span being replaced (long tool output is clipped, your messages and the agent's are
  not), carries the previous summary forward, and writes notes with fixed sections for your
  standing instructions and approvals (quoted word for word), the agent's commitments and its
  pending scheduled actions. The notes are labelled as the agent's own, not as a message from you.
  A long compaction now makes one summarizer call per chunk.
