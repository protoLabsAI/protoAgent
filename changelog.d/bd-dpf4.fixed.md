- **The chat composer's idle placeholder names the actual agent (#4713).** The main chat
  composer hard-coded "Message protoAgent…" regardless of how the agent was named, so a renamed
  agent (whose header and ⌘⇧K palette chat already read its own name) still said "Message
  protoAgent…" in its composer. The idle placeholder now reads "Message `<agent name>`…" using the
  same display name the header and palette chat use (`brandName` of the runtime identity), falling
  back to "Message the agent…" when no name is known. The mid-turn "Steer the agent…" and ↑-recall
  queued-steer hints are unchanged.
