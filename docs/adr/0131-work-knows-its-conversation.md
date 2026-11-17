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

## Amendment 2026-11-17: audience partitions and scheduling

- Status: Accepted by Josh as direction (2026-11-16, answer 5 "as recommended": record the private-steering design here as an amendment rather than a new ADR). The Phase 1 implementation proof is authorized and in progress (Josh's Phase 1 go, 2026-11-17); **CONTINUE — the decision that would carry this past the bounded Phase 1 proof slices into production — has not been made.**
- Date: 2026-11-17
- Deciders: Josh Mabry; protoAgent maintainers
- Source: the [Conversations in Work proposal](../design/conversations-in-work-proposal.md), "Design revision after Phase 0" sections 2–4 and "Decisions recorded 2026-11-16" (including the decision-1 clarification). That proposal records the Phase 0 (bd-h6gka) REVISE verdict, the Model B recommendation, and the four Phase 0 issues (#5400–#5403) this design answers.

This amendment records the private-steering design for Conversations in Work on the stores ADR 0131 already commits to. It does not change D1–D6, the phases, or any other ADR; it extends them with how a private audience is identified, stored and scheduled. Nothing here authorizes the build beyond the Phase 1 proof slices named in the proposal.

### A1 — Principal kind identifies the human operator, carried separately from trust tier

The human operator for private steering is the console's **shared operator bearer plus the operator's own paired devices of kind `device`** (phones and browsers). The **fleet service token, paired devices of kind `agent`, and the federation token are peers** for private content, whatever operator trust tier they otherwise earn. This matches decision 1 as clarified by Josh 2026-11-16 ("devices that I've authorized for myself, like my phone … should be 'operator' as well"; `agent`-kind devices stay peers).

The trust tier alone cannot make this distinction: `_classify_token` gives every paired device, the shared bearer and the fleet service token the operator tier, so a tier check cannot tell the human's phone from an agent's hub. The **device kind can**, and it is fixed when the pairing code is minted and copied onto the device at claim time, never from anything the claimer sent — a claimer cannot relabel an `agent` code as `device`. Therefore a **verified principal kind** (shared-bearer, `device`-kind device, fleet-service, `agent`-kind device, federation) is propagated from `_classify_token` and the device record **alongside and separate from `trust_tier`**, and private reads and sends gate on principal kind, not on tier. Issuance, rotation and revocation reuse the existing device lifecycle (ADR 0087 pairing, ADR 0121 D5 rotation-with-reuse-detection); **no new console-only credential is introduced.** The device kind proves a credential was paired for the operator's personal device use, not that a human is present or *who* holds it — the accepted risk recorded under "A4" below, not a weakening of decision 1.

### A2 — Private steering lives in console-only partitions; the original identity is kept

Private steering and its replies **must not live in the shared stored conversation**: not in SDK `Task.history`, not in shared artifacts, not in shared checkpoint `messages`, and not in their compaction archives. They live in a **versioned console-only payload in server-only columns on the existing task records** (not wire-visible SDK metadata), plus **private checkpoint state accessible only to the trusted turn runner**. A raw peer checkpoint/history API never returns private channels; the public runner receives a projection with those channels absent.

These are **storage partitions of the same original conversation, not a side chat, fork or summary-seeded copy.** The original identity is kept — one owning-agent/context identity, resolved original thread ID, ordered turn IDs and task links — and the console assembles the authorized timeline from the public and private partitions. Peers receive only the public projection and their own authorized releases: no private placeholders, text, counts or private task IDs. A private continuation may read public context; a peer model call reads only public context and released results. Private intake (idle start, resume, loopback) is accepted transactionally before SDK history acceptance; a missing private record, audience version or resolver binding stops continuation rather than falling back to an ordinary SendMessage. The proposal’s decision 3 applies to both partitions: × deletes the original including its private payloads and checkpoints, and no automatic sweep deletes a linked original while linked work is active.

### A3 — One audience per execution segment, durably scheduled per original thread

Each execution segment has **one audience, assigned before its first model or tool call and immutable for the rest of that segment.** The scheduler is **durable and runs one serialized runner per resolved original thread** — a projection of durable task state, not a second work scheduler or binding database. Accepted inputs are persisted as pending turns on the existing task records with a monotonic acceptance sequence, audience, verified principal, message-id deduplication key, checkpoint binding, interrupt ID and queued/running/parked/terminal state, ordered with no cross-audience batching.

Private steering **waits for a safe boundary and peer work queues behind it.** A private steer arriving during a peer turn queues; at the next safe model/tool boundary the peer execution yields, commits its public checkpoint and closes its segment, and a distinct private segment then runs on the same original. Peer inputs arriving while a private turn runs likewise queue and are never drained into the private call. Reading the original's historical **public** context from a private turn is still allowed — "one audience" governs newly accepted inputs and all output of the segment, not a ban on reading the public request. A **parked segment reserves the runner until its matching guarded resume** (or explicit cancellation); a matching resume is a continuation of that segment, not a later queue item, and if a human's private answer resumes a peer segment, the suspended public segment is closed at the interrupt and a private successor segment is persisted on the same task/checkpoint binding before model execution, preserving ADR 0129 D8's guarded decision controls. Ordering uses a transactional compare-and-set on the active segment/generation, not only a process-local lock; recovery reloads the durable queue and audience before claiming a runner, and an unknown execution outcome stays suspended for reconciliation rather than replaying an effect. A long park can delay peers; the operator sees a content-free waiting state and can cancel or resume explicitly.

### A4 — Private steering stays enabled everywhere; console-authority risk is accepted

Private steering **stays enabled everywhere**, per Josh's 2026-11-17 decision. The principal-kind seam cannot distinguish a human from an agent that obtains console authority or holds a `device`-kind pairing; **that risk is accepted**, rather than making credential and tool isolation a deployment prerequisite. Steering text and private replies must never reach peer channels or peer-readable history. This partitioning does not stop the model repeating what it was told.

### Open questions (not decided here)

- **Result release.** Whether a private-derived result may be released to peers automatically remains **open** — Josh has not decided question 1. This amendment does not settle the result-release contract or change the proposal's decision 2.
