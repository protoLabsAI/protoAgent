- **A burst of inbox items from one source now runs as one turn, so the agent replies once.**
  When the retry-when-free / now-recovery pass claims several pending items with the same
  source, priority and origin (a webhook retry storm, a run of sister-agent messages, a
  boot backlog), it sends them as one self-A2A turn. Each item's text is labelled with its
  inbox id and keeps its own late-delivery note. Retry dedup still holds: the batch gets a
  deterministic identity (ADR 0129 amendment), recorded on every member's execution row,
  so a retry after an uncertain send joins the accepted turn. Acceptance, restore and
  retirement apply to every member, and StormGuard counts the batch as one fire. A single
  item is delivered exactly as before. Fixes #5519.
