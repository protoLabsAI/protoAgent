# ADR 0131 — Work knows its conversation: continuity and progress on the stores we already have

- Status: Accepted (2026-11-15, Josh: "yep lets do it"). **Committed scope: P1 and P2.** P3–P5 are planned but re-decided after P2 ships, using real data.
- Date: 2026-11-15
- Deciders: Josh Mabry; protoAgent maintainers
- Implementation: P1 and P2 boarded 2026-11-15. P3–P5 not boarded.
- Builds on: [ADR 0129](./0129-work-consolidation.md) (tasks, D8 answer-in-place, D9 driven/stall), [ADR 0130](./0130-server-authoritative-live-sync.md) (durable change feed), `docs/design/other-conversations.md` (the From others tab).
- Umbrella issue: #5299. Its audit comment (2026-11-15) and the due-diligence section in the issue body are the evidence base.

## Context

#5299 proposed a continuity and observation layer: durable work-to-channel bindings, durable event intake, an evidence-based progress read model, an observe-only supervisor, and channel policy. Due diligence and an audit of core `e210e6e` / projectBoard `be7f10a` found that most of the mechanism already exists, and that the failures we actually hit come from a few specific gaps.

**What already exists (verified 2026-11-15):**

- **Inbox delivery lifecycle** (`src/inbox/store.py`). Dedup keys, one durable execution identity per item reused on retry (`inbox_executions`), capped recovery (`RECOVERY_MAX_ATTEMPTS`, `RECOVERY_MAX_AGE`), and `recovery_accepted_at` so an accepted item survives a crash. Items carry a free-text `source` and an optional `target_session`. There is no structured origin.
- **Task links** (`src/tasks/store.py`). Tasks link cards, PRs, jobs and directives. `resolve_link` closes a task when every link is done, and the board now publishes `task.link.closed` (pb#770, with a backfill).
- **Board `/status`.** Reports `merge_holds` (an explicit expected wait, with reason and age), `claim_stall`, `preflight` / `held_projects`, `stale_base_checkouts` and `orphaned_cards`.
- **D9.** `tasks/stall.py` provides the shared driven / idle / stalled test, a durably deduplicated nudge, and parking to `waiting`.
- **Alert paths.** Board block pages (pb#735) page the inbox with evidence and `needs_human`, deduplicated.
- **Thread origin.** A2A threads store display-only origin (`a2a_impl/conversation_origin.py`), and `GET /api/conversations` returns it (v0.244.0).
- **Discord.** Every turn is persisted in SQLite (discord-plugin `turn_log.py`, table `discord_turns`).

**What actually broke (2026-11-11 → 15):**

1. A task created in a `job:` / inbox / scheduler turn takes `session_id` from that throwaway turn (`tools/scheduler_tools.py`, `_session_id_from(state)`). Its Needs-you answer then has nowhere to go. ADR 0129 D8 had to move answering onto the item because, on 11-14, all four of protoEngineer's Needs-you tasks pointed at sessions with zero turns.
2. Outside A2A threads were deleted after 24 h while the From others tab promised 7 days (`a2a_impl/stores.py` `sweep_expired_tasks`). Fix carded as bd-0txnt. Any binding hung off an A2A task row would have vanished with it.
3. Card-linked tasks never closed, because nothing published `task.link.closed`. Fixed by pb#770.
4. The board skipped every card silently for four hours while its base checkout sat on another branch. Fixed by pb#768, with the delegate side fixed in #5305.
5. The signed `/ci` and `/review` routes would have applied stale or duplicate results. This is latent, since nothing calls them, and is fixed by pb#771 (head SHA required, applied once per head).
6. The follow-up backoff registry is in memory (`scheduler/followup_backoff.py`, `_BACKOFF: dict`), so after a restart quiet follow-ups re-fire before backing off again.
7. Delegation results get lost: #5317 (background ACP turn ends with no reply) and #5318 (a foreground ACP timeout returns no collectable task id).
8. A Discord thread's session changes on restart **and** when a conversation expires: the key is `discord-channel-{thread}:{conversation_id}` and the map is in memory.

None of these needed a new task system, binding table or event log. Each needed an existing store to carry one more fact, or keep it long enough.

## Decision

### D1 — Extend the stores we have; add no new binding store or event log

Josh, 2026-11-15: "yes extend vs new storage." The binding from work to the conversation behind it is **the task's own origin plus its links**, persisted in the tasks store. Events keep flowing through the inbox and board reconciliation; polling stays the authoritative fallback. New facts go into existing tables as columns or metadata. Nothing gets a parallel queue, a global session, or a second lifecycle store.

### D2 — A task's origin is the conversation that caused it, never a throwaway turn

When a task or card is created inside a `job:`, `background:`, inbox or scheduler turn, its origin is **inherited from what caused that turn**: the inbox item's originating session or context, the scheduled job's `origin_session`, or the delegation's caller context. It is never the `job:` id itself. Origin stays display and routing data only, never an authorization input (as in `conversation_origin`). A task with no recoverable origin keeps an empty origin, and D8's answer-in-place still works.

**How (amended 2026-11-15 after mapping the code).** The origin is **resolved from the durable stores that already record the cause**, at the moment work is created. It is not threaded through graph state. Scheduler fires and background delegations never pass through the inbox. They run directly on `job:` / `background:` contexts whose cause is already stored: `jobs.origin_session` joined through `job_sessions` (context id to job), and `BackgroundJob.origin_session`. Inbox-fired turns get the same from a new inbox `origin_session` column joined through `inbox_executions`. A resolver (`tools/session.py`) maps a `job:` / `background:` context to its originating session, recursing a few hops while the answer is still a server context. With no match it returns empty, never the `job:` id. The resolved origin is written into the task's existing `session_id`, so D8's reply routing and the C6a provenance link work unchanged. Tasks get no new column. Board cards gain an origin field through the plugin, which calls the host resolver.

### D3 — Outside conversations live for the window the console shows

Josh, 2026-11-15: "7 days is fine." A context that `GET /api/conversations` would list is kept for at least the conversations window (7 days). The list and the sweep share one definition of the excluded prefixes and the window (bd-0txnt). `job:`, `background:`, `system:` and `palette-` keep the 24 h TTL. Console chats keep their tombstone rule.

### D4 — Progress is a view over existing signals, not a new model

"Is this work moving, waiting, or stuck?" is answered by **joining** board `/status`, D9's `is_driven` / `task_activity`, and the Work view. Expected waits are the existing explicit ones: `merge_holds`, task status `waiting`, the follow-up backoff's "still waiting on you", and preflight holds. Only gaps get new signals, by extending D9's driven test with progress evidence the board already records (a fix round with no commit, a verdict that won't change at the current head). Activity alone is never progress, and quiet is never stalled by itself.

### D5 — Supervision is observe-only and speaks through existing alert paths

Any new detector **records and alerts only**. It never dispatches, cancels, requeues, edits branches, resolves reviews or merges. It emits through board block pages or the inbox with `needs_human`, deduplicated durably. D9's existing parking of a task to `waiting` stays: it acts on a task, never on code, and it is the documented exception.

### D6 — Measure before piloting

Before any new detector is enabled, its outcomes are counted in `metrics.db`: thread-to-work routing after restart, stale or duplicate events suppressed, stall alerts raised and how each resolved (true stall vs expected wait), and repeated notifications. Thresholds are chosen from a week of those numbers, not before.

### Out of scope

- **An out-of-process wedged-host watchdog.** The fleet hub knows its members' pids and ports, but probes only on demand and has no health loop or alert path. Our incidents were stalled cards, stranded checkouts and lost threads, not wedged hosts. If a wedge happens, the cheapest addition is one card: the hub polls each member's `/healthz` with a timeout and, while the pid is alive but health fails N times, pages the inbox (deduplicated).
- A new task system, a binding table, an event log with consumer acks, automatic merges or remediation, cloud compute, and rewriting memory.
- Delegation-result loss (#5317, #5318). These are bugs, fixed on their own; P4's measurements will show whether they recur.

## Phases

Each phase is boarded as small or medium cards (four files or fewer each, per the board's breadth gate), after acceptance.

- **P1 — Close the known gaps** (most are already carded or fixed):
  - pb#771 merged.
  - bd-0txnt: outside threads kept 7 days (D3).
  - The follow-up backoff registry persisted, so a restart doesn't re-fire quiet follow-ups (new card).
  - #5317 and #5318.
- **P2 — Work knows its conversation (D2):**
  - P2a-1. Inbox items carry `origin_session` and the display origin fields. `InboxStore.add(origin_session=, origin=)`, `origin_for_context(context_id)` through `inbox_executions`, and `POST /api/inbox` accepts them.
  - P2a-2. D9 stall nudges pass the stalled task's `session_id` as the inbox item's origin.
  - P2b-1. Origin resolver: `job:` goes to the inbox, then the scheduler (`job_sessions` joined with `jobs.origin_session`). `background:` goes to the background store. Bounded recursion, empty when unknown.
  - P2b-2. `task_create` and `schedule_task.origin_session` (not its `context_id` or `wait`, which decide where a fire runs) use the resolver, so a task created in a cron or inbox turn binds to the chat that caused it.
  - P2b-3 (projectBoard). Cards record an origin through the host resolver, and board block pages pass it to the inbox. Needs a core floor bump once P2b-1 ships.
  - P2c. Show the binding both ways. API: `GET /api/conversations` rows gain the open tasks whose `session_id` is that thread. UI: a designSystem spec and preview first, then cards.
  - P2d (Discord). After a restart or conversation expiry, a known thread is restored from `discord_turns` (same `conversation_id`, origin channel recovered), and the gateway's thread gate checks the turn log on a miss, so the thread doesn't fall back to a plain channel that needs an @-mention.
- **P3 — Progress view (D4):** a read-only join of board `/status`, D9 and Work, plus D9's driven test extended with the board's progress evidence. No new store.
- **P4 — Instrumentation (D6):** the counters above, recorded for a week.
- **P5 — Observe-only pilot (D5):** alerts for the real cases first: a preflight hold past N passes (pb#768 pages, measure it), waiting on a verdict that can't change at the current head, and a card claimed with no commit past its budget. Each alert links its evidence and resolves with one recovery notification.

## Consequences

- No migration of work into a new system. P2's schema changes are additive columns and metadata on the inbox and tasks stores, with defaults that keep today's behaviour.
- D8's answer-in-place stays the fallback for tasks with no recoverable origin.
- The board stays the only scheduler for code. Nothing here gives an external event, a thread link or channel membership any authority.
- Revisit the wedged-host watchdog only after a real wedge.

## Acceptance scenarios (named tests at slice time)

- A task created in a scheduled job's turn, an inbox-fired turn or a background delegation turn gets the session that caused it, never the `job:` id, and keeps it across a restart (P2b).
- A From others thread stays listed and readable for 7 days, and is gone after 8 (P1, bd-0txnt).
- A Discord thread keeps one origin across a restart and across a conversation expiry (P2d).
- A restart doesn't re-fire a backed-off quiet follow-up (P1).
- A duplicate or stale `/ci` result at the current head applies at most once (P1, pb#771).
- Quiet tests, an approval wait, a chatty no-change worker and an unreadable store give four distinct progress readings (P3).
- A pilot alert raised before a restart isn't raised again after it, and its recovery notifies once (P5).
