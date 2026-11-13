- **Interrupted turns: the Needs-you recovery item (ADR 0129 D7 S7, #5149).** Every
  occurrence a restart leaves `interrupted_needs_operator` now raises one neutral,
  server-recorded decision in the job's execution session — naming the job, the step
  count and the last tool call ("… was interrupted by a restart after N steps. Last
  action: tool(args), and its effect is unknown.") — which renders in Work's Needs you
  through the existing decision path. Resume continues from the checkpoint with the
  dangling call reported as interrupted (the original task handle is reopened — or
  re-minted under the same id when the 24h TTL sweep already deleted the failed row —
  so the drive's real outcome lands on it; a Resume whose checkpoint no longer holds
  the turn is refused with 400 and the card stays answerable, never silently converted
  to a failure); Re-run starts one fresh turn from the job's prompt
  through the scheduler's normal claim+fire path (the only answer that starts a new
  turn — new occurrence, new message identity, never a resend); Drop settles the
  occurrence as dropped without running anything. With no (or a stale) checkpoint only
  Re-run and Drop are offered. Answers act exactly once — the decisions-ledger CAS
  arbitrates concurrent answers (the loser gets 409) and every occurrence transition is
  CAS'd on the held state — and dismissing the card never resumes anything. Each S6
  auto-resume now also appends an Activity entry: "Resumed &lt;job&gt; after a restart
  (step N)".
