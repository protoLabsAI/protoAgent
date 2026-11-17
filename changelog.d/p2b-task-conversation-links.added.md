- **Tasks can record the conversations behind them.** The tasks store now accepts
  related-conversation links — `{ref: "conv:<owner>/<context_id>", kind: "conversation",
  role, owner, context_id, state, reason}` — in the existing `links` JSON, so a later
  phase can open the right original transcript with its role (requester/coder/reviewer/
  related) and owner-agent identity. One original carries several roles as a single
  deduped `roles` entry, two owners sharing a context are two distinct links, and an
  unknown role or a missing owner/context_id is rejected. Like plain URLs, these links
  are provenance only: they never block, drive or get resolved for task completion.
  Store-only, no API/tool/UI change. Part of the Conversations in Work work (ADR 0131).
