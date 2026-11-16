# Conversations in Work: original chats, private steering, retained history

Status: **proposal for team review; not implementation authorization.**
Prepared 2026-11-15 (America/Los_Angeles). Code inspected at
`d9eadcc05d8cbadd6c5d79a656a978a7c69168eb`. Backend findings below are verified by
source inspection only; runtime scenarios and cited tests were **not executed**.
Keep the proposal PR open under **merge hold**; do not auto-merge. Approval to
review this document is not permission to build, release, or create implementation
cards. This change adds this file only and leaves approved specifications intact.

## Decision sources and approval boundaries

The operator approved the direction in the proposal request: put progress and
related conversations in Work, remove the standalone From others tab, open the
original conversation, remove the duplicate Answer in chat action, keep steering
and its private responses operator-only, and retain original history while linked
work is active. The agent may still deliver intended work results to peers.

The supplied design references are **spec `a-1794812909598-cd8b26` v2** and
**preview `a-1794812482334-68d817` v4**. These are artifact references, not durable
repository files. Their substantive design is captured below; review does not
depend on an expiring preview URL. Visual sign-off does not establish backend
readiness, authorization, privacy, retention, or actual-device acceptance.

| Evidence category | What it establishes |
| --- | --- |
| Operator-approved direction | Work-to-original-Chat navigation; one Open in chat action per available related conversation, including Needs you; private steering and private responses; active-work history retention. |
| Existing accepted design | [ADR 0129](../adr/0129-work-consolidation.md) defines four Work groups, job identity and execution ownership, recovery, and answer-in-place. [ADR 0131](../adr/0131-work-knows-its-conversation.md) commits P1/P2, extends existing stores, and treats origin as provenance, not authority. Its P3–P5 remain subject to later decisions. |
| Existing layout approval | [work-view.md](./work-view.md) supplies row, rail, inspector, phone, focus and failure rules. [work-consolidation.md](./work-consolidation.md) replaces its old group structure with Needs you, Doing now, Coming up and Your instructions. |
| Historical conversation approval | [other-conversations.md](./other-conversations.md), revisions 1–5, describes the pinned read-only From others tab, Continue in a new chat, and links between threads and jobs. That approval remains recorded; this proposal explicitly replaces that interaction rather than claiming it meets the new contract. |
| Newer accepted architecture | [ADR 0130](../adr/0130-server-authoritative-live-sync.md) makes server state and resync authoritative. [ADR 0132](../adr/0132-chat-context-carryover.md) covers memory/digests and checkpoint recovery, not full-history preservation or private peer conversations. [ADR 0133](../adr/0133-model-roles-with-fallback-chains.md) covers model chains, not conversation audiences. |
| Preview proposals, not separately confirmed decisions | Routine threads in a Started by others group in the Chat list; the preview's completed-work presentation; unknown output audience defaulting to private. These need the product or technical decisions below. |

The existing four-group design already says finished work leaves active Work.
That does not approve the preview's specific destination for completed history
or routine conversations. Likewise, visual approval of a Needs you row does not
silently amend ADR 0129 D8's reply-in-place fallback for missing origins. The
approved new flow has **no Work conversation composer**. Before implementation,
reviewers must explicitly reconcile that replacement with D8, preserving native
structured decision controls and a truthful recovery path for unavailable chats.
This document does not edit D8 or authorize removing its current behavior.

## The proposed experience

Work answers what is happening and which conversations explain it. Main Chat is
where the operator reads and steers the owning agent. No fork, copied history,
summary-seeded new chat, or new Work composer satisfies this proposal.

### Work list, requests and promotion

Keep the four Work groups and their shared counts. Actionable incoming requests
may appear as Work items before a task, job or board card exists. Show the request,
its state, and **Asked by** with the original requester and known provenance.
An automated conversation alone does not establish that there is actionable work;
request eligibility remains an open product decision.

When a request becomes a task/job/card, promote it **in place**. Preserve its
logical Work identity, Asked by, original conversation links and navigation
target. Record the transition in its history. A request and the job satisfying it
must not become two rows or inflate counts during retries, refresh, reconnect or
restart. A board card link is progress evidence for that work, not another copy
of the request. One request producing several independent jobs needs an explicit
parent/child and count policy; do not deduplicate those jobs solely by conversation.

Show real progress, expected waits and uncertainty using existing signals. A
stream of events is not proof of progress, and a quiet conversation is not proof
of a stall. Keep the ADR 0131 distinction between progress views and supervision;
this proposal adds no automatic dispatch, remediation or merge authority.

### Related conversations and original Chat

The Work detail has a **Conversations** section. Each row names its role and
participant, for example **Requester · Claude Code for Josh**, **Coder · Codex**,
or **Reviewer · Vera**. Show state as a word and dot, last activity time, and
message count when known. Unknown counts stay unknown. A role describes the
relationship to the work; it is neither a credential nor proof that the thread
can be steered.

Each available row has exactly one conversation action, **Open in chat**.
Needs you uses the same action; there is no separate **Answer in chat** button.
Opening does not send a message, answer a decision, restart a task or grant
authority. Multiple roles pointing to one original should be represented without
duplicate open controls for that same conversation. Different requester, coder
and reviewer originals remain distinct.

Open or reuse the **original persisted conversation** in main Chat with the same
owner-agent/context identity, original history, live task/progress and speaker
attribution. Repeated opens select the same chat. The header provides **Back to
the job in Work**; before promotion, use corresponding request wording. Return
restores the Work item, group, filters, scroll position and surface frame. If work
finishes while Chat is open, keep the conversation and an honest finished detail
on return; do not switch to unrelated work or resurrect an active count.

The composer names the **owning agent**, not the requester or peer. It explains
that operator steering and the agent's private responses stay private. This
promise must not ship until the audience boundary is proven. Sending a new turn
within the same original context is allowed; minting a replacement conversation
is not. A parked decision must use its matching guarded resume path, not a new
prompt that bypasses the interrupt.

### Desktop, phone and accessibility

Desktop rail: opening a Work item pushes its detail in the rail, with Back to the
group and preserved scroll. Expanded Work: a selected item opens the inspector
beside the list at the existing wide-layout threshold; below 48rem of container
width, use push/back. Expand and Dock preserve selection and focus. Open in chat
reveals main Chat rather than putting another transcript or composer in Work.

Phone: Work is the full-screen sheet with pushed details, no Expand/Dock controls,
rows at least 48px tall and full-width stacked `md` actions. Open in chat leaves
the sheet for the original main Chat; Back restores the same detail. Keep drafts
bound to the owning conversation through agent changes and navigation. The
preview's Started by others Chat-list group is a candidate replacement for
routine-thread discovery, not an approved prerequisite silently added here.

Use the existing DS rows, `Badge`, `StatusDot`, `Callout`, `Alert`, `Skeleton`
and `Button`; no new DS components or tokens are proposed. The supplied preview
covers 12 states across list, rail/expanded detail, original Chat, request before
and after promotion, Needs you, finished, unavailable, loading, failed and routine
conversations, on desktop/phone and in light/dark themes.

Keep semantic headings and lists, text status in addition to color, and counts
in accessible heading names. Give repeated Open in chat controls contextual
accessible names identifying the role and participant. Do not nest a button
inside a clickable row. Move focus to the Chat heading when opened, then back to
the originating row on return, or the nearest heading if that row disappeared.
Escape closes an expanded inspector before its dialog; sheet/dialog focus is
trapped, rail focus is not. Announce loading and promotion once in a polite live
region, failures appropriately, without announcing every progress frame. Verify
keyboard order, reduced motion, 390px layout and 400% zoom without sideways scroll.

### Loading, failures and missing history

| State | Proposed behavior |
| --- | --- |
| First load | Skeleton rows with a loading announcement. Do not flash zero counts or claim there are no conversations. |
| Refresh fails | Keep last-known rows, mark them stale and offer Retry. A failed source does not blank other Work groups. Revalidate authorization and capability before send. |
| Opening fails or owner is offline | Keep Work selection and any existing draft; show a scoped failure and retry the same original. Do not create a replacement chat. |
| Original no longer stored | Show an unavailable row with no Open action and explain the known loss. No copied fallback. This is for lost, unavailable or remote data, not permission to age out an actively linked original. |
| Permission denied | Explain lack of access without disclosing protected transcript content; do not route through another agent or weaker transport. |
| Partial transcript | Label it partial, distinguish paging from actual loss, and load older retained turns where supported. A 50-turn initial page is not the whole history. |
| Missing checkpoint | Reading retained history may remain possible, but steering is non-resumable until original checkpoint binding/recovery is verified. Do not present empty or summary-only context as full continuity. |
| Finished work | Conversation can remain open; return shows the terminal state and no active count. Exact completed-history discovery remains open. |
| Nothing linked | Say no related conversations are recorded. Do not invent a requester, child thread or recoverable origin. |

If an approved visual state cannot preserve identity, privacy and history, revise
that state before building it. In particular, remote read access alone cannot
justify an enabled composer, and missing original history cannot justify Continue
in a new chat as a fallback.

## Current code: findings and limits

All findings here are **inspection-only, unexecuted** at the checkout above.
The supplied earlier inspection at `3371584f0` is context, not test evidence.

| Area and source | Finding and implication |
| --- | --- |
| Running steering: [chat_routes.py](../../src/operator_api/chat_routes.py), [server/chat.py](../../src/server/chat.py), [middleware/steering.py](../../src/graph/middleware/steering.py) | POST `/api/chat/sessions/{session_id}/steer` calls `steer_or_start_turn`. A running or parked session queues input. Middleware drains it into a `HumanMessage` before the next model call and emits `steer_consumed` with queued text. This continues the original session, but does not make that text private. |
| Peer delivery: [executor.py](../../src/a2a_impl/executor.py) | The `steer_consumed` handler publishes its payload through the task updater and mirrors it to the progress hook. `_flush_text` and terminal artifact emission also publish the answer. Removing text from the consumed event alone leaves answer and persisted-history exposure paths. |
| Idle and completion races: [server/chat.py](../../src/server/chat.py) | `_session_turn_idle` checks activity and pending interrupts under a thread lock. `_start_steer_turn` sends a loopback `SendMessage` with the same `contextId` and a new task; duplicate message IDs join the started task. Failed starts requeue. This is not a copied chat, but it does not prove privacy or restart-safe audience selection. |
| Checkpoint binding: [thread_ids.py](../../src/graph/thread_ids.py), [hitl_routing.py](../../src/a2a_impl/hitl_routing.py) | Default checkpoint identity is `a2a:<session_id>`, but plugins can resolve it from request metadata. Idle checks resolve with no metadata; the loopback includes selected turn settings, not arbitrary original metadata. Same context alone therefore does not prove same checkpoint under custom resolvers. Parked routing and ownership must be validated together. |
| Ownership: [stores.py](../../src/a2a_impl/stores.py), [hitl_routing.py](../../src/a2a_impl/hitl_routing.py) | Task owners partition operator and federation callers; operators can also read federation tasks. `check_context_owner` rejects foreign-context continuation for nonoperators. This is task-level protection, not a per-message boundary between private operator responses and peer deliverables on an existing task. |
| Existing open helper: [openSourceChat.ts](../../apps/web/src/work/openSourceChat.ts), [sessionHydration.ts](../../apps/web/src/chat/sessionHydration.ts) | The helper hydrates durable turns for the same ID, restores dismissed sessions, pushes reopen when dismissed, selects Chat and returns false on failure. Reuse these seams. The helper accepts a session ID, not an owner-agent/context pair; it is not sufficient for remote routing. Hydration starts with `SESSION_TURN_LIMIT = 50` and needs full-history paging and speaker/audience review. |
| Chat index and registration: [chat_routes.py](../../src/operator_api/chat_routes.py), [session_registry.py](../../src/server/session_registry.py) | Turn-based session discovery filters `chat-%`; the server-held registry also supports explicit registration and reopen. Opening a non-chat original locally is not a durable discoverability contract. Register explicitly opened originals and verify resync, fresh-device and restart behavior; do not expose every job/system session by removing the prefix filter globally. |
| Incompatible continuation: [chat-store.ts](../../apps/web/src/chat/chat-store.ts), [ConversationReader.tsx](../../apps/web/src/chat/ConversationReader.tsx) | `continueConversation` calls `createSession` and seeds a draft in a new chat. The reader offers Continue in a new chat. Neither implements the approved original-chat flow. |
| Current Needs you: [tasks.tsx](../../apps/web/src/work/sections/tasks.tsx) | Waiting-task details contain a Reply form and source-history checks, while shared provenance renders Started in. Replacing the conversation reply flow requires explicit D8 reconciliation; this documentation changes none of it. |
| Origin and work: [tools/session.py](../../src/tools/session.py), [tasks/store.py](../../src/tasks/store.py) | `origin_session_for` follows durable inbox/scheduler/background causes with bounded recursion and empty-on-unknown behavior. Tasks persist `session_id` and links; links normalize reference/kind/state/reason and can close tasks when resolved. Origin is not a unique work ID or a list of role-labelled original child transcripts. One conversation can cause multiple tasks. |
| Delegation continuity: [conversations.py](../../plugins/delegates/conversations.py) | `_CONTEXTS` and `_PENDING` are process-local bounded `OrderedDict`s (512 entries). They cannot be the durable source of related-thread links or retention references. A caller's `origin_session` is not the delegated child's transcript identity. |
| Retention: [conversation_policy.py](../../src/a2a_impl/conversation_policy.py), [stores.py](../../src/a2a_impl/stores.py) | The shared outside-conversation window is seven days. `sweep_expired_tasks` works on individual task timestamps, preserves console chats until tombstoned, and protects paused/restart-spared tasks. Excluded job/background/system/palette contexts normally retain the 24-hour TTL. These rules do not establish a complete linked-conversation active-work keep. |
| Pruned checkpoints: [chat_rehydrate.py](../../src/server/chat_rehydrate.py) | Merged #5365 supplies summary-plus-recent-turn recovery for an aged-out checkpoint. It does not prove every original turn is retained, private responses are isolated, or a metadata-dependent remote original is safely resumable. |

## Required backend contracts, proposed for implementation review

### Private input and private response audiences

Preserve one original conversation identity, with server-enforced audience-aware
reads and delivery. Private steering and its private agent responses must be
operator-only even when a peer already holds the running task's stream, poll
handle or push subscription. The agent may send the intended work result to the
peer without forwarding private instructions, acknowledgements or discussion.

Enforce this at every output surface: streaming frames, GetTask/poll results,
push callbacks, durable task history, transcript reads, progress events, tool
outputs, artifacts, exports, and memory/search recall that can reach another
audience. Client-side hiding and a prompt telling the model to be discreet are
insufficient. Model output can quote private input, so filtering only input events
is insufficient too. Persist audience decisions and separate deliverable output
from private conversational responses within the existing storage architecture.

The DS recommendation to default unknown output audiences to private is a
**proposal requiring technical validation**, including how authorized peer work
results still finish normally. It is not a claim that today's transport supports
this. Prove behavior for running, idle, parked, resume, completion races, retry,
restart, and subscribers established before the steer. Do not use a side thread
to obtain privacy at the cost of losing the original identity.

Explicit relay such as “tell them the deadline moved” is a separate permission
and UI decision. Recommendation: make the intended recipient and exact relay
scope clear, authorize that delivery, and record it without disclosing unrelated
private exchanges. Do not infer permission to forward the entire steering turn.

### Identity, provenance and persistence

Extend existing task/inbox/job/session records and their links; add no binding
database, parallel work queue or event log (ADR 0131). Proposed facts to persist:
stable request/work correlation, promotion target and deduplication key, immutable
requester/provenance, and related-conversation references with owner agent,
context ID and role. Use existing durable change events and authoritative refetch
for both Work and Chat. Schema and cross-store ordering need a separate technical
review; these fields are requirements, not a newly approved schema.

Idempotent promotion must commit correlation before exposing the promoted row.
Clients must resolve old request links to the same work and derive counts from
one shared projection. Repeated inbox delivery, promotion retries, partial writes
and a restart must not create another identity. Do not infer equality from titles,
timestamps, participant names or origin alone. Preserve unknown provenance as
unknown, and distinguish a work-result link from the actual related transcript.

An owner-agent/context pair determines routing, but does not grant access.
Authenticate operator reads and sends at the owner; bind cache/query/session keys
to the owner so equal context strings on two agents cannot collide. Verify remote
capabilities separately for read, full history, live attachment and steering.
Reject unsupported interaction visibly. Reusing local APIs through a remote
proxy must preserve authorization, checkpoint metadata and audience guarantees.

### Active-work retention

**Approved invariant:** preserve the full linked original history while **any**
linked work remains active. After the last linked work closes, apply normal
closed-chat retention. A recent tail, summary or active task row is not a substitute
for the earlier conversation. Retain the original transcript/archive chain and
the checkpoint, ownership and links needed for safe continuation; do not rely on
browser caches or delegation memory.

Recommended policy, still to decide: treat every nonterminal work state as active,
including queued/Coming up, blocked, waiting/Needs you, parked and unresolved
acceptance/recovery. Hold references conservatively when a store cannot establish
closure. Reconcile durable references before pruning on restart. A shared original
stays retained until all linked jobs close, even if its last message is older than
seven days. A task turn ending, tab closing, request promotion, or one child closing
must not release the other references.

Apply the keep to the **entire original**, including older terminal task rows,
compaction archives and required checkpoint state. Coordinate task TTL and
checkpoint pruning (see [checkpoint_prune.py](../../src/graph/checkpoint_prune.py));
protecting only the latest task would still lose the opening request. Persist
terminal release and reconcile idempotently so restart cannot release early or
keep abandoned references forever. Release makes the history eligible for normal
policy, not an instruction to delete it immediately.

The precise closed-chat clock, explicit erase behavior while work is active, and
remote retention guarantees need agreement with existing close/delete semantics.
Do not promise deletion and silently retain data, or silently delete active work's
history. A remote owner unable to guarantee the keep needs a capability limitation
and design revision before that state is advertised as supported.

## Overlap check and proposed small phases

Live GitHub reads during preparation found [#5365](https://github.com/protoLabsAI/protoAgent/pull/5365)
**merged** at `2026-11-16T07:21:45Z` (November 15 locally); its merge is this
checkout's HEAD. [#5366](https://github.com/protoLabsAI/protoAgent/pull/5366)
(model chains) and [#5363](https://github.com/protoLabsAI/protoAgent/pull/5363)
(prior-session digest filtering) were the open PRs returned. They are separate
work, but recovery, privacy and session discovery changes must be checked against
their final state before implementation. Do not treat the older report that
#5365 was open as current.

Live board status was **not verified**: no board tool was exposed, and the bounded
read-only probe of `127.0.0.1:7870/api/plugins/project_board/status` could not
connect. Therefore this is not a “no overlap” conclusion. Recheck the live board,
ADR 0131 P2 work and open PRs before assigning implementation. No cards are created
by this proposal.

| Proposed phase | Small deliverable and dependency | Exit evidence |
| --- | --- | --- |
| 0. Resolve contracts | Agree open product decisions, D8 amendment boundary, remote capability and audience model. Refresh board/PR overlap. | Explicit scope approval and named unsupported states; no implementation inferred from visual sign-off. |
| 1. Audience isolation | Implement and persist private input/response versus peer deliverable audiences in existing transport/store seams. Depends on 0; precedes interactive navigation. | Adversarial stream/poll/push/history tests with preexisting peer handles; authorized result delivery still works. |
| 2. Durable work relationships | Add request correlation, role-labelled original links and idempotent promotion to existing stores. Can be designed with 1; no interactive UI enabled. | Retry/restart/multiple-job fixtures prove identity, provenance and counts; owner authorization rejects forged links. |
| 3. Retention and continuation | Protect all linked originals; reconcile references and closure; validate checkpoint metadata binding and #5365 interaction. Depends on 2. | Full-history and shared-link tests across TTL boundaries and restarts; missing checkpoint fails safely. |
| 4. Original-chat navigation | Extend openSourceChat/hydration/registry with owner routing, durable reopen, capability checks and return-to-Work state. Depends on 1–3. | Fresh-client, reconnect, owner switch, missing data and parked-resume scenarios pass without a new chat. |
| 5. Work presentation and migration | Add conversation rows/request promotion presentation, replace From others entry points and retire duplicate conversation actions. Depends on 4 and approved routine/completed discovery. | Desktop/phone/accessibility review plus focused integration tests; old links still resolve safely. |
| 6. Controlled rollout | Pilot on supported local owners, then separately qualified remote owners. | Operational evidence for private delivery, correct counts, durable reopen and retention; explicit release approval. |

These are review-sized phases, not implementation cards or dispatch instructions.
Split each at its API/store/UI seam after approval rather than bundling backend
privacy and a visual replacement into one release.

### Migration, rollout and rollback

Use additive versioned metadata/columns and idempotent backfill in existing stores.
Recover relationships only from durable evidence. Keep unmatched records readable
with unknown links; never synthesize copied transcripts or guess coder/reviewer
identity from the requester's origin. Journal cross-store stages using existing
facilities so a crash can resume migration before sweeps or promotion run.

Preserve old `#/conversations/<id>` links as compatibility routes to the authorized
original or its truthful unavailable state. Migrate only explicit opens into the
Chat registry; do not register every hidden/system session. Removing the standalone
From others tab must wait for an approved route to routine conversations, including
unopened ones, so discoverability is not lost during migration.

Roll out behind capability/feature gating, with operator-authenticated, bounded,
redacted, read-only diagnostics. Measure original-open failures, unexpected new
identities, promotion duplicates/count mismatches, retention holds/releases and
audience refusals without logging private message content. Do not retroactively
claim already-published peer text can be made private by migration.

Rollback disables new interaction and keeps protected history and durable links.
Do not roll back to a binary that ignores retention pins or audience metadata.
Keep the safety layer, disable pruning if necessary until reconciliation is safe,
and use read-only presentation for affected originals. Never restore a copied-chat
fallback or expose private responses through the old reader. Retiring the old UI
and applying any ADR amendments require a reviewed implementation change.

## Open decisions for the team

| Question that remains open | Recommendation for review, not approval |
| --- | --- |
| How does an operator explicitly authorize relay to a peer, and what does the UI show? | Identify recipient and scoped message/result; record explicit permission separately from private steering. |
| Which nonterminal states count as active for retention? | Include queued/Coming up, blocked, parked, waiting and unknown recovery; release only on durable closure of all references. |
| Can one request produce several jobs? | Preserve a request lineage and distinct child work IDs; decide whether the parent counts before implementing promotion/counts. |
| What makes an incoming thread an actionable request rather than routine conversation? | Require durable work intent/evidence; do not classify every A2A thread as Work or guess from a title. |
| Can a remote original be read, retained and safely steered? | Advertise verified owner capabilities separately; allow read-only where authorized, with no composer until privacy and continuation are proven. |
| Where are routine threads discoverable after From others is removed? | Evaluate the preview's Started by others group in Chat; approve its eligibility and visibility before removing the old entry point. |
| Where does completed work/history appear? | Keep finished work out of active counts as already designed; choose the history destination and stale return-link behavior explicitly. |
| How does the new no-Work-composer flow replace D8 when origin/history is missing? | Keep structured decision controls; design a recovery state without fabricated chats. Amend the approved contract in a later review before removing the current fallback. |
| How do last-closure retention, explicit erase and existing tab close semantics interact? | Define the retention clock and truthful erase UX; coordinate active-work protection with ADR 0132 memory/incognito rules. |
| Is default-private output technically enforceable without blocking intended results? | Prototype and test audience selection in the original session; revise the design if the backend cannot meet both obligations. |

## Focused validation and risks

Existing test files inspected or located as validation seams, **not run**:

| Existing test | Relevant coverage to preserve/extend; not proof of the new contract |
| --- | --- |
| [test_steer_idle_starts_turn.py](../../tests/test_steer_idle_starts_turn.py) | Idle start, running/parked queues, duplicate IDs, completion races, failure/requeue and loopback context. Add audience persistence and process-restart cases. |
| [test_steering.py](../../tests/test_steering.py) | Queue/drain and middleware injection. Add private responses and safe intended-result delivery, not just event redaction. |
| [test_chat_routes.py](../../tests/test_chat_routes.py) | Operator routes, session history and listing seams. Add owner authorization, explicit original registration and private history reads. |
| [test_work_origin.py](../../tests/test_work_origin.py) | Durable cause resolution, bounds/cycles and missing origins. Add related child transcript provenance and idempotent request promotion. |
| [test_a2a_stores.py](../../tests/test_a2a_stores.py) | TTL, paused/restart preservation and durable stores. Add all-linked-active references, terminal release, seven-day edges and partial transcript loss. |
| [openSourceChat.test.ts](../../apps/web/src/work/openSourceChat.test.ts) | Reopen/store wiring and leaving the Conversations stage, with mocked hydration. Add owner routing, failed reads, durable reopen and return state; mocks alone do not prove native backend continuity. |
| [test_checkpoint_prune.py](../../tests/test_checkpoint_prune.py) | Checkpoint pruning/recovery seam from #5365. Add linked-history retention and unsupported recovery handling without treating a summary as the original transcript. |

Required missing scenarios before enabling interaction:

1. A peer holds stream, poll and push handles before private steering. Neither the
   steering text nor private response appears on any peer surface, including
   artifacts, history or tool output; the intended result still reaches the peer.
2. Operator history reads succeed; unauthorized peer reads and forged owner/context
   links fail. Equal context IDs on different agents never share caches or transport.
3. Exercise running, idle, parked, resume, completion race and process restart with
   a metadata-dependent thread resolver. Verify the same checkpoint identity,
   message deduplication and audience after each transition.
4. Missing checkpoint, unavailable remote owner, partial history and a failed read
   produce distinct honest states. No path creates a chat or sends a replacement
   prompt. Recovery through #5365 does not hide missing earlier turns.
5. Open the same original twice, reload, reopen on another device, miss change-feed
   events, resync and return to Work. Identity, drafts, speaker attribution and
   progress remain correct; a stale local tab is not counted as persistence proof.
6. Retry promotion across a crash and stale refresh; keep one identity and count.
   Test two jobs from one request separately once that count policy is approved.
   Coder/reviewer links must reach their own original transcripts.
7. Keep an old original with two linked jobs across the seven-day boundary and
   multiple restarts. Close one job, then the last; verify complete history remains
   until release, and ordinary retention only becomes eligible afterward. Include
   queued/blocked/parked states once decided, archived spans and old terminal turns.
8. Verify desktop rail/expanded and phone navigation, single action per row,
   keyboard/focus return, light/dark, draft retention and loading/failure states.
   Preserve real tap regression coverage: a passing click or emulated iPhone test
   does not establish physical-device WebKit acceptance. Verify selection,
   Escape/reopen and focus after asynchronous agent/model switching.

Target console scenarios in [work-view.spec.ts](../../apps/web/e2e/work-view.spec.ts),
[work-mobile-layout.spec.ts](../../apps/web/e2e/work-mobile-layout.spec.ts),
[chat-status-rail-needs-you.spec.ts](../../apps/web/e2e/chat-status-rail-needs-you.spec.ts),
[chat-park-reload.spec.ts](../../apps/web/e2e/chat-park-reload.spec.ts) and
[chat-continuity.spec.ts](../../apps/web/e2e/chat-continuity.spec.ts), extending only
the relevant specs. Future changes should run targeted pytest, Vitest related
files, and those Playwright specs; no full local e2e run is required by this
proposal. This documentation-only change adds no behavioral tests.

The largest risk is a convincing private UI over a shared output channel. Next
are false continuity from a bounded transcript or wrong checkpoint, premature
retention release, duplicate work after promotion, and losing routine threads
when removing their only entry point. The phase gates address those risks before
the visual replacement ships. Retention also grows storage; monitor size without
silently weakening active-work protection. Missing isolation evidence is a gate
on readiness, not proof of a new defect in every consumer.

## Team-review checklist

- [ ] Confirm the approved original-Chat flow and single Open in chat action,
  including Needs you; no copied-chat fallback or Work conversation composer.
- [ ] Confirm Asked by/requester provenance, role-labelled related originals and
  promotion without duplicate identity/counts; decide multi-job semantics.
- [ ] Resolve routine-thread eligibility/discovery and completed-history placement.
- [ ] Approve the explicit relay contract and technically validate private input,
  private responses and intended peer-result delivery on every output surface.
- [ ] Agree active-state, last-closure, erase and remote-retention semantics.
- [ ] Resolve D8 replacement and remote/missing-checkpoint limitations explicitly;
  request a design revision for states that cannot meet the invariants.
- [ ] Accept additive migration/rollback on existing stores, durable original
  registration, owner authorization and server-authoritative resync.
- [ ] Refresh the live board and PR overlap check before any implementation cards.
- [ ] Review focused backend evidence plus desktop/phone/accessibility acceptance;
  do not confuse inspection, mocked tests or visual approval with runtime proof.
- [ ] Keep this proposal under merge hold until explicit review disposition.
  Separately approve specification amendments, implementation scope and release;
  none is authorized by this document.
