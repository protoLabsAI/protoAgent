- **Durable one-audience-per-segment scheduling (dormant).** Phase 1 proof slice P1c for
  Conversations in Work: a serialized runner per resolved original thread whose accepted inputs
  persist as ordered pending turns on the existing task records (sequence, audience, principal,
  message-id dedupe key, checkpoint binding, interrupt id and state). A segment's audience is
  fixed before its first model/tool call; the steering middleware's drain is scoped to that
  audience, so no model call ever mixes a peer and a private input. Completion versus acceptance
  is decided by one transactional compare-and-set on the active segment's generation, not only a
  process lock, and a real process restart reloads the durable queue, audience and order with no
  replayed tool effect and no public fallback. The mechanism stays dormant — only the P1c tests
  exercise it; there is no user-reachable private-steering surface and no disable switch. Refs
  #5400, Refs #5401.
  Acceptance and segment creation are serialized by a database transaction; queue recovery and
  deduplication cover the complete private partition. Folded turns are attached atomically so
  they cannot dispatch twice, and proven dispatch refusals release and requeue the same input.
