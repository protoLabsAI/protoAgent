# ADR 0070 — Background results: push-resume, indexed reports, disposable workers

- **Status:** Accepted (2026-07-01)
- **Date:** 2026-07-01
- **Deciders:** Josh Mabry; protoAgent maintainers
- **Tags:** background, memory, knowledge, a2a, console
- **Supersedes / Superseded by:** amends ADR 0050 (background subagents); composes
  with ADR 0069 (memory delivery layer)

> A background job's report currently waits for the operator to happen to message
> the origin thread again (`notified=0` can sit forever), while the full report is
> memorized under the *worker's* identity — `memory/background:bg-*.json` carries
> the untruncated report into every thread's `<prior_sessions>` digest, and
> retirement harvest attributes it to `a2a:background:*`. This ADR flips delivery
> to **push**, re-keys capture to the **origin session**, and makes worker
> transcripts **disposable**.

## 1. Context & Problem statement

ADR 0050 got the registry right: every job row carries `origin_session`, the full
report persists in `background_jobs.result`, and the notified-gated drain
(`server/chat.py:_drain_background_messages`) is exactly-once. Three gaps remain:

1. **Pull-only delivery.** The drain fires on the origin session's *next* turn.
   Nothing at the completion moment (`server/a2a.py:_handle_background_terminal`,
   which holds both `origin_session` and the full result) wakes the origin agent —
   so reports strand undelivered and the operator is never engaged.
2. **Wrong memory attribution.** The worker runs as a full lead-agent turn under
   `background:bg-*`, so `SessionSummaryMiddleware` persists its transcript (full
   report in `final_output`), the digest loader has no surface filter, and
   `conversation_harvest` + fact extraction run against the worker identity.
   The worker's transcript has no long-term value once the report is delivered;
   the report's value belongs to the origin session.
3. **Context economics.** The drain injects up to 6 000 chars of report into the
   origin thread; a 15k-char report is unusable in-context and unsearchable
   out-of-context.

## 2. Evidence

Due-diligence survey (2026-07-01) of async result delivery across Claude Code,
OpenAI (background mode / Deep Research / Tasks), LangGraph, Letta, Devin, Manus,
Cursor:

- **Push-inject-and-wake is the frontier and only Claude Code ships it**: background
  task completion injects a notification and wakes the origin loop with no user
  message, queue-and-fold when busy. Everyone else polls, webhooks to *your*
  infrastructure, or notifies the human and parks the result.
- **Summary-in-thread + full-body-in-store + retrieval-on-demand is unanimous**
  (Anthropic subagent contract returns a 1–2k-token distilled summary and forbids
  reading the worker transcript; OpenAI `file_search`; Manus restorable
  compression — keep the pointer, drop the body; Letta block-vs-archival).
- **Worker transcripts are retained-but-disposable everywhere** (Claude Code
  30-day cleanup; LangGraph `on_run_completed: delete`; Letta deletes data-source
  workers on completion). The main agent never reads them; it consumes the
  summary and *resumes the worker* if more is needed.

## 3. Decision

- **D1 — Push-resume.** On terminal completion, after `mark_complete` + the bus
  event, the server submits a minimal self-A2A nudge into `origin_session` (same
  self-POST mechanism the spawner uses), so the origin agent runs a turn, the
  existing notified-gated drain injects the `<task-notification>` (exactly-once
  preserved), and the agent briefs the operator against the new data. Guards:
  never for `background:*`/incognito origins, never-raises, config
  `background.auto_resume` (default **on**). Mid-turn origin sessions fold the
  nudge via the existing steering queue.
- **D2 — Index the report, shrink the injection.** At completion the full result
  is indexed into the knowledge store keyed to the **origin session**
  (`source=origin_session`, `source_type="background_report"`, heading carries
  description + job id; trust tier 2 — agent-derived). The drain notification
  shrinks to a summary-sized cap with an explicit pointer: full report searchable
  via `memory_recall`, openable by job id in the document viewer.
- **D3 — Disposable workers.** `background:*` sessions are excluded from session-
  summary persistence, from the `<prior_sessions>` digest loader (also filters
  legacy files), and from retirement harvest/fact extraction (mirroring the
  incognito skip). The worker's checkpoints remain until normal pruning — debug
  window, never memory.
- **D4 — Report card + by-id route.** `GET /api/background/{id}` replaces the
  list-and-filter fetch. The chat report card becomes a real card: raised surface
  (`--pl-color-bg-raised`, not the near-black inset pill), drop shadow, clamped
  excerpt with a bottom fade-out mask, and a primary "Open report" CTA into the
  document viewer. Specificity stacked (`.pl-message--system.chat-report …`) so
  the DS default can't win by load order.
- **D5 — Fan-out batch-join (#1766).** D1 push-resumes *per job*, so a fan-out of N
  background subagents (`task_batch(run_in_background=True)`, or several
  `task(run_in_background=True)` in one turn) fired N separate briefing turns — the
  real cross-report synthesis only landed on the last. D1 now **coalesces a fan-out
  into one push-resume**: every job a single turn spawns is tagged with a `batch_id`
  = **the emitting assistant turn's id** (`graph.agent._turn_id_from`), carried through
  `spawn`→`store.create` on the new nullable `background_jobs.batch_id` column. On each
  terminal completion the server calls `manager.resume_for_terminal(job)` instead of
  `resume_origin`: a singleton (no batch / `batch_size ≤ 1`) push-resumes unchanged; a
  member that is not the last to settle is **held** (returns `None` — nothing delivered,
  not a failure); the **last** member (`batch_outstanding == 0`) wins a synchronous
  single-fire claim (`_joined_batches`) and fires **one** `resume_origin_batch` whose
  nudge summarizes the fan-out with per-status counts (`batch_status_counts`, e.g.
  `completed 6, failed 1`) — the drain (already per-session) then attaches *all* sibling
  reports to that one turn for a single briefing. A failed member counts as settled and
  appears in the summary. A **straggler timeout** (`BACKGROUND_BATCH_JOIN_TIMEOUT_S`,
  default 900s) is armed on the first held completion and forces a *partial* join if a
  member hangs, so finished reports can't be stranded forever; the remaining reports still
  drain on the session's next turn. Incognito batches never reach the join (the D1
  `_should_auto_resume` guard skips them before `resume_for_terminal`). The drain and its
  exactly-once guarantee are unchanged.

## 4. Consequences

- Reports become events the origin agent *acts on*, not mail waiting for pickup;
  the `notified=0`-forever failure mode disappears.
- The `<prior_sessions>` digest stops carrying worker transcripts (less noise,
  no full-report leak through `final_output`), while the report itself becomes
  durably searchable with correct provenance — visible in the Memory inspector,
  trust-tiered, superseded-aware (ADR 0069).
- A completed job now costs one extra agent turn (the briefing) — the point of
  the feature; `background.auto_resume: false` restores pull-only.
- Amends ADR 0050's "surfaced on the next turn" contract to "surfaced by a
  triggered turn"; the drain mechanism and exactly-once guarantee are unchanged.
- The pipeline is plugin-reachable (#1635): `graph.sdk.spawn_background(…)` spawns a
  job that rides D1–D4 for free, and `graph.sdk.background_status(task_id)` reads the
  jobs-store row (`report` once terminal) — no more reaching into
  `STATE.background_mgr` (ADR 0043 consumption SDK).

## 5. Alternatives considered

- **Deliver the full report into the origin thread.** Rejected: context rot;
  15k-char reports are why the 6 000-char cap exists. Summary + search is the
  cross-system consensus.
- **A bespoke `search_report(job_id, query)` tool.** Rejected: the knowledge
  store already provides FTS, provenance, trust tiers, and the inspector;
  a second search stack would duplicate all four (ADR 0021 one-store rule).
- **Webhook/external notification only (OpenAI/Devin model).** Rejected: notifies
  the operator, not the agent — the stated goal is the agent receiving the report
  and working it with the operator.
- **Keep worker summaries but filter the digest only.** Rejected: the summary
  file still duplicates the full report on disk with no consumer; the jobs DB is
  the system of record.
