# ADR 0129 — Work consolidation: four things a person tracks, and an agent tool surface to match

- Status: Accepted (2026-11-11). Josh approved the direction ("hold and fold into the redesign") and answered Q1–Q3 the same day (see "Decisions taken").
- Date: 2026-11-11
- Deciders: Josh Mabry; protoAgent maintainers
- Implementation: background execution isolation is specified below, not implemented by this documentation slice. The console designSystem spec is approved (2026-11-12, v2); slices follow below.
- Amends: [ADR 0127](./0127-compaction-fidelity.md) D5/D6 (pending actions and directives); D6 below also reconciles ADRs 0003, 0053 and 0079. Touches the scheduler, watch, task, goal and decision tool families.

Code citations are `origin/main` after v0.236.0. **(unverified)** marks inference not yet confirmed from code.

## Context

The console's Work panel has eight sections (`apps/web/src/work/sections/`): decisions, tasks, background, schedules, watches, goals, directives and activity. Each one mirrors a subsystem. The agent sees the same split as about two dozen tools:

| Concept | Agent tools |
|---|---|
| Schedules | `schedule_task`, `list_schedules`, `cancel_schedule`, `wait` (`tools/scheduler_tools.py`) |
| Watches | `create_watch`, `update_watch`, `clear_watch`, `list_watches` |
| Tasks | `task_create`, `task_list`, `task_update`, `task_close` |
| Goals | `set_goal`, `update_goal_plan`, `abandon_goal` |
| Decisions | `request_decision`, `withdraw_decision`, `ask_human` |
| Directives | `record_directive`, `list_directives`, `fulfil_directive`, `revoke_directive` (`tools/directive_tools.py`) |
| Inbox | `check_inbox` |
| Background jobs | subagent delegation run in the background (unverified: exact tool names) |

Josh, 2026-11-11: "this entire work/activity panel is a bit much. schedules, tasks, goals, watches, directive. like its just an overload for a user and the agent."

Evidence from the same day:

- **A directive that says nothing.** mythxEngineer answered a decision ("run the #877 merge queue?") by clicking an option. It then hand-recorded directive `dir-cfd234` with the quote `"confirmed"`, which is a word the operator never typed. The console card leads with that quote. What it actually authorizes is in small print.
- **The decision and the directive duplicate each other.** The answered decision already records who approved what, and when.
- **A watch contradicted the newer approval.** A watch prompt still said "never merge #864–#871" after the operator confirmed merging them. The agent had to notice the conflict and resolve it by itself.
- **Raw internal text reached the operator.** Board pages, delivered as priority-now inbox items, rendered as "undelivered page" alerts full of internal diagnosis (projectBoard-plugin#735).
- **Decisions in a background session went unseen.** One raised in `system:activity` sat for 70 minutes, and answering it looked frozen (#5099).

## Decision

### D1 — Four groups in Work, for the operator and the agent alike

Everything the agent is tracking falls into one of four groups. A fifth concern, history, lives outside Work:

1. **Needs you**: decisions, and items that need a human (for example, a stuck delivery). This is the only group that asks for attention.
2. **Doing now**: jobs. Background jobs, the active goal and what used to be tasks are all jobs (Q1).
3. **Coming up**: anything that will run later, whether set off by a time (schedule, `wait`) or by a condition (watch).
4. **Your instructions**: the operator's standing instructions and approvals.
5. **What happened** is **not** a Work group (Q3). History is already recorded by telemetry, traces and the activity log, and is read there.

The console's Work panel renders the four groups with one row primitive, using the kind (schedule, watch, goal, job…) as a tag. The in-chat status rail summarizes groups 1–4 with the same vocabulary and the same counts. The visual design is a designSystem spec that Josh approves before any build.

### D2 — One "follow up later" primitive

Schedules, watches and `wait` collapse into one concept: a **follow-up** with a trigger, either a time or a condition, plus a prompt and an owner chat. The agent gets one tool family (create, list, update, cancel) instead of three. Existing schedule and watch records migrate. The old tool names stay as thin aliases for one release so forks and plugins can move, then they are removed.

### D3 — A goal is a job with a finish line

A goal stops being a separate subsystem from the operator's point of view. It shows in **Doing now** as a job with its completion criteria and its plan. The goal loop's internals (ADR 0073) are unchanged. `set_goal` and `update_goal_plan` stay, but their records surface through the same row and inspector as other jobs.

### D4 — Approvals come from answers, not agent paraphrase (amends ADR 0127 D6)

- **An answered decision is a standing approval, recorded by the server.** Its headline is the decision's question and the chosen option, and it links to the decision. The agent does not call `record_directive` for a decision answer.
- **`record_directive` stays for free-text instructions.** The server rejects a `quote` that does not appear verbatim in the operator's messages for that turn.
- **The display leads with what is approved.** The headline is the scope. The operator's words show beneath as provenance ("You said 'go for it' · 16:07").
- **A directive tied to a bounded job closes itself** when the job finishes (Q2). It is recorded as fulfilled with the job as the reason. A directive with no job attached stays in force until it is fulfilled or withdrawn.

### D5 — Fewer, clearer tools for the agent

The prompt's capability map groups the tools under the same four headings, and each heading gets one paragraph on when to use it. The target is a third fewer tools in the always-bound set. Aliases do not count toward the target.

### D6 — Background job sessions (S0, Refs #5110)

Activity is a provenance feed, not the shared runner for unrelated work. This
amends [ADR 0003](./0003-reactive-agent-activity-thread.md)'s single Activity
checkpoint decision while retaining its authenticated intake, event bus, dedup
and storm controls. It refines [ADR 0053](./0053-wait-yield-and-resume.md)'s
background continuation ownership; deliberate interactive same-chat waits remain.
It also amends [ADR 0079](./0079-autonomous-operating-model.md)'s working-state
scope and [ADR 0127](./0127-compaction-fidelity.md)'s D5 scope without weakening
compaction fidelity. These are contracts for follow-on implementation, not claims
about isolation already present in the runtime.

This is core work, not project_board. Josh's hands-off #5099/#5106 symptom fixes
are independent: this chain does not touch, attach to, requeue or depend on them.
No release is authorized here; Josh tests Work before the next release.

#### Source grounding

Paths below are relative to `src/`, inspected in this checkout.

| Boundary | Current behavior and required correction |
|---|---|
| `scheduler/interface.py::Job` | `context_id` selects execution; `origin_session` selects result delivery. They must remain separate. |
| `scheduler/local.py` | `jobs.db` contains `jobs` and `occurrences`. `_claim_occurrence` commits message identity and a job snapshot before sending and advances the cron slot. `_fire` selects `job.context_id or ACTIVITY_CONTEXT`. `_remember_task`, `_reconcile_occurrences` and `_settle_task` preserve accepted handles and distinguish pending/unknown from terminal work. Isolation must preserve this ledger. |
| `tools/scheduler_tools.py` | One-shot `schedule_task` targets the injected session; cron leaves context null; both stamp origin. `wait` uses `wait:<session>` (fallback `wait:activity`) and yields only on success. Watches persist `run_session`. |
| `graph/sdk.py` | `run_in_session` explicitly targets a session and replaces a pending one-shot by stable job ID. `react_on` defaults explicitly to `system:activity` with debounce/coalescing. Changing only the null fallback misses it. |
| `server/a2a.py` | `_fire_activity_from_inbox` uses Activity and a new message ID per attempt. `_scheduled_delivery_payload` reads origin from the live job and suppresses wait/same-chat duplicate reports. `_on_a2a_turn_terminal` routes Activity/peer results to the feed but other contexts to chat surfacing. Context changes alone would lose feed routing. |
| `graph/projection.py` | `_schedule_scope` prefers context over origin and treats other recurring jobs as agent-wide. A dedicated context alone would hide the origin chat's full pending instruction. |
| `graph/decisions.py`, `graph/decision_resume.py`, `operator_api/decision_routes.py` | The decision ledger stores a session. Answering uses `hitl_resume` against that checkpoint, with decision-ID validation under the turn lock. Input-required is not occurrence completion. |

#### Four identities and stable naming

| Identity | Durable contract |
|---|---|
| Job | Scheduler `id` plus generation `created_at`, scoped by instance and `agent_name`. Recreating an ID creates a new generation; editing or pausing does not. |
| Execution session | `job:<execution_uuid>`, assigned once and mapped durably to the job generation before claim/send. A2A checkpoint thread is `a2a:job:<execution_uuid>`. Recurring fires reuse it through restart, edits and pause/resume. Never derive it from title, prompt, origin or cron slot. |
| Occurrence | Preserve `(job_id, next_fire, created_at)`, stable `message_id`, accepted `task_id`, and immutable dispatch-context snapshot. Each fire is distinct even when it reuses the job session. |
| Origin chat | Nullable `origin_session` is provenance/result destination, never an implicit request to execute in that chat. Closing/deleting it does not redirect execution. |

Human labels are `Scheduled job: <title>` or `Inbox: <source>`, with a short
job/item ID when needed to distinguish rows. Renaming a label never renames its
context; UUIDs are not primary Work headlines.

Null and explicit `system:activity` background targets allocate isolated owners.
Explicit non-Activity targets remain deliberate continuations: interactive
`wait`, same-chat one-shot `schedule_task`, goal/watch wakes and
`run_in_session(chat, …)` retain their context. `react_on` keeps its signature,
debounce and stable pending replacement key; its Activity default requests an
isolated background owner. Replacement may replace pending work only: retain an
accepted predecessor's occurrence and queue its replacement behind the same
owner, with a distinct generation/claim. Unsupported remote backends must report
that they cannot honor isolation, not silently fall back to Activity; existing
signatures remain usable.

Wait/watch continuations persist an explicit root-job-generation and execution
owner, plus parent occurrence when known. Their trigger identity is separate
from their owner's session. A background wait uses `wait:<execution_session>`;
two jobs from one chat cannot replace each other's waits. A watch's `run_session`
and owner point to that same execution session; hook-only watches remain hook-only.
Origin provenance and fences propagate. Read ownership from injected graph state,
not the empty tool-body tracing contextvar. ADR 0053's interactive one-pending-wait
per chat, full conversation continuity, success-only yield and resumed-chat
delivery remain intact.

#### Serialization, lifecycle and retention

At most one active invocation owns an execution session. Hold ownership across
input-required, unknown acceptance and outstanding wait/watch continuations,
even when the initial A2A turn ends after yielding. The owner's continuation can
enter after its predecessor yields for it; a new independent cron occurrence
cannot. Decisions resume the existing interrupt under its lock, not a new prompt.
Other jobs remain runnable.

Do not queue every missed cron tick. While held, keep the next due slot; when
released, claim at most one due occurrence and compute the next slot after claim
time, matching existing coalescing behavior. Preserve startup stale-fire, TTL,
max-fires, backoff and pause policies, except none may drop unresolved work.
Migration itself never recomputes times. Terminal settlement updates counters
exactly once; yield or a pending decision does not mean the logical job finished.

Distinguish terminal turn, terminal occurrence and finished job. Recurring jobs
remain live between fires; one-shot roots remain unfinished while continuations
exist. Cancellation/expiry stops future triggers but cannot erase accepted work
or imply its cancellation succeeded. Retain an owner record after deletion of
the one-shot scheduling row.

Checkpoint, task handle, owner mapping, occurrence snapshot and required archive
history are pinned beyond normal TTL while any occurrence is unresolved, decision
resolution unconsumed, or continuation outstanding. Failed resume delivery and
missing task reads retain those pins. Origin-chat archival is independent. Only
after all references resolve may existing retention policies archive the session;
keep identity/settlement tombstones as long as replay/dedup can reference them.
Never reuse retired execution UUIDs. Archival never authorizes resume or resend.

#### Idempotent migration and restart

Run versioned migration before polling, inbox dispatch and reactive hooks. Use
guarded additive schema changes and transactional owner assignment in `jobs.db`.
Checkpoint, task and decision stores are not one transaction: journal migration
stages durably and resume incomplete stages on boot; exclude older dispatch
writers during migration. Do not migrate by cancel/re-add, checkpoint copy or replay.

1. Inventory jobs **and orphan occurrence snapshots**, accepted tasks, legacy
   waits/watches and parked decisions. Allocate one mapping per independent job
   generation targeting null or exactly `system:activity`. Keep future routing
   separate from original accepted dispatch context. Preserve explicit non-Activity
   targets and origins. Do not copy shared Activity history into new sessions.
2. Preserve occurrence keys, `message_id`, `task_id`, `job_snapshot`, `expected_next`,
   state and diagnostics. Preserve job IDs, `created_at`, `next_fire`, `last_fire`,
   enabled state, fence, TTL/max-fires, counters and outcome timestamps. Only
   routing/migration metadata changes; a second migration is a no-op. New routing
   applies to the next unaccepted invocation, never an accepted Activity task.
3. Reconcile accepted work through its original task handle/checkpoint. Claims
   without recoverable handles stay quarantined; missing/failed GetTask reads
   never authorize resend. Only positive existing retryable/non-acceptance evidence
   permits normal retries. Settlement remains transactional and generation-guarded:
   an old task cannot delete or increment its replacement. Resolve legacy accepted
   work before admitting its owner's new independent fire.
4. For `wait:system:activity` and null-origin `wait:activity`, bind to a job only
   when durable creation/occurrence/checkpoint evidence identifies exactly one
   owner. Prompt wording, timestamps, origin chat or last-seen job are insufficient.
   A proven, unaccepted wait may target that owner with explicit continuation
   state; retain its old row/key as an alias until settlement instead of resetting
   occurrence identity. Accepted waits reconcile at their original target.
5. Without ownership evidence, preserve the wait as a held legacy continuation
   with its original due time/prompt, visible through existing Needs you delivery
   handling. Never drop it, guess a new owner or auto-fire into shared Activity.
   An operator may bind/rearm positively unaccepted work or cancel it. Unknown
   acceptance requires reconciliation first; rearming cannot bypass it. This is
   visible unresolved work, not a claim to reconstruct unavailable ownership.
6. Legacy parked decisions retain ledger IDs, session, task and checkpoint
   interrupt. Resume the matching decision in place using the existing guarded
   path; never clone a checkpoint or resubmit the original prompt. Retain a
   restricted legacy drain lane for these resumes, admitting no new independent
   Activity fires. If records disagree with the live interrupt, only the matching
   record may resume; expose others as unresolved. A missing checkpoint is a
   recovery item, not an answered decision. Follow-up waits from this drain need
   proven ownership or the same hold policy.

Uncertainty therefore stays durably visible rather than losing pending work or
duplicating accepted work. This does not promise exactly-once external tool side
effects; it preserves the existing no-blind-resend acceptance boundary.

#### Job-scoped state, compaction and attribution

ADR 0079/0127 working-state now uses explicit execution ownership. A job sees its
own goal/plan, tasks, directives, pending waits/watches and current occurrence,
with full-instruction caps, cancellation labels and `[FIRING NOW]` retained.
Other jobs get bounded summaries/counts, not their actionable instructions as
this job's commitments. Origin chats still see their own scheduled actions in
full: replace context-or-origin precedence with separate membership tests.
Plugin work providers remain projections, not replicated state; agent-wide
summaries must not imply ownership of unrelated commitments.

ADR 0127's single engine, archive-first ordering, whole-span fold, intact tool
pairs, verbatim operator messages, continuation sections and failure behavior
remain. Auto/manual/overflow compaction use the job checkpoint and archive
namespace. Repeated compaction preserves its approvals, stop conditions and
continuations without importing sibling or origin-chat histories. Keep configured
system guidance and explicitly scoped directives; consult the authoritative
ledger for revocation. Wake prompts never become operator authorization. Tool
fences survive scheduling/resume. Conversation isolation is not a filesystem sandbox.

Fire/result/decision/feed records carry execution session, job generation,
occurrence/message/task identity and nullable origin provenance. Independently
fired inbox work persists `inbox:<item-id>` → execution UUID and acceptance record
before send; retries reuse message identity and accepted task handle. Distinct
items from one source get distinct sessions. Source names and dedup windows are
not execution IDs. HTTP success alone is not delivery evidence; uncertain sends
are reconciled, not retried with new IDs. Existing next/later pulls and
`deliver: thread` information-only delivery do not create independent runners.

Activity routes by origin/ownership, not context equality, and deep-links to the
actual execution/result. Preserve legacy feed history. Scheduled reports retain
origin delivery and first-card/subsequent-chip behavior; same-chat waits retain
duplicate-report suppression. Snapshot attribution before one-shot deletion.
Deduplicate feed/report retries by occurrence/task independently of invocation;
missing origin or failed feed writes never trigger another execution.

Reuse the [approved Work spec](../design/work-consolidation.md): all-chat Work,
origin-chat meta/This chat badge, shared counts and the existing fleet-wide Needs
you marker. Job-session decisions remain visible without an origin chat; answers
route to the execution checkpoint, not displayed origin. Not-started jobs stay
Coming up; started blocked jobs stay Doing now with their decision in Needs you.
Finished items leave Work; history stays Activity/telemetry. No new layout,
group, marker or inspector design is authorized here.

#### Exact follow-on tests

These are required future tests, not coverage claimed by this documentation slice.
Use temporary real stores and deterministic clocks/transport barriers for restart
and concurrency; consumer payload fixtures alone do not prove native isolation.

| Suites | Required cases and assertions |
|---|---|
| `tests/test_scheduler_local.py` | Two null and two explicit Activity jobs get distinct sessions; repeat/restart/edit/pause retain them; recreated IDs get new generations. Pending/unknown/input-required blocks only its owner. Wait continuation runs while the next cron stays held; release coalesces missed ticks once. |
| `tests/test_scheduler_local.py`, `tests/test_scheduler_ttl.py` | Migrate twice and crash after every journal stage. Compare preserved columns, keys, snapshots and handles byte-for-byte. Include accepted Activity tasks, orphan snapshots, retryable/unknown sends, replacement jobs and duplicate terminal settlement. No extra SendMessage, reset counter/next_fire or lost pending work. Missing GetTask stays held; cancel/TTL cannot purge unresolved work. |
| `tests/test_scheduler_tools.py`, `tests/test_sdk_scheduler.py`, `tests/test_sdk_react_on.py` | Interactive wait/one-shot retains chat history and one pending key; background waits cannot replace siblings. Watches retain owner/fence. Null and explicit SDK Activity isolate; explicit chat/goal targets, debounce and pending replacement still work. Accepted replacement retains its predecessor. Unsupported backend fails visibly. |
| `tests/test_decision_needed.py`, `tests/test_decision_expiry.py` | Two jobs park independently; answer/dismiss/expiry affects only the selected checkpoint. Legacy matching interrupt resumes in place once; foreign/missing interrupts cannot resume another job. Unattributed wait is held; proven unaccepted wait rebinds without key reset; accepted wait never replays. Restart retains all unresolved references. |
| `tests/test_inbox.py` | Same-source now-items get distinct contexts; duplicate delivery of one item reuses durable identity. Crash before/after acceptance, transport loss and missing task evidence do not create another invocation. Pull tiers and thread delivery stay unchanged. |
| `tests/test_working_state.py`, `tests/test_working_state_wiring.py`, `tests/test_compaction_fidelity.py`, `tests/test_system_thread_compaction.py` | Put sentinels in actual live origin, Activity and sibling histories. Inspect native provider-bound messages before and after a real tool execution: only explicit job inputs/own history survive. Verify configured guidance, effective fence bindings, own full pending instructions, origin visibility and current-fire marker. Run auto/manual/overflow compaction twice; own verbatim approvals/stop rules survive, sibling sentinels do not. Verify identity/receipt readback from durable stores. |
| `tests/test_activity_emit.py`, `tests/test_activity_feed.py`, `tests/test_scheduler_failed_turn_wire.py` | Dedicated-context success/failure reaches feed and origin once after one-shot deletion, reconnect and failed-delivery retry. Actual context/task links and collapse counts persist; same-chat wait has no duplicate report. Missing origin does not lose history. |
| `apps/web/src/work/workGroups.test.ts`, `apps/web/src/decisions/entrypoints.test.tsx`, `apps/web/e2e/decision-needed.spec.ts`, `apps/web/e2e/chat-decision-resume-live.spec.ts` | Existing groups/counts, origin labels and fleet marker include isolated and legacy-held decisions. Answer resumes only the selected execution while another job progresses. Finished items leave Work; Activity remains history. Run these relevant browser specs and Josh's desktop Work validation before release. |

Implement durable identity/migration first, then scheduler/SDK/inbox dispatch and
continuation ownership, then state/compaction/result/decision attribution with
integration validation. Background-session S0 is separate from the console slice
numbering below.

## Consequences

- **Good:** one mental model for operator and agent. Fewer wrong-tool calls and fewer contradictory records (a watch versus an approval). Work becomes scannable, and decisions stop hiding in background sessions.
- **Cost:** migrating schedule and watch records. A release cycle of aliases. Plugins that register Work sections (`registerWorkSection`) need to choose a group (unverified: which plugins do).
- **Risk:** merging schedule and watch semantics could lose a capability one of them has (for example, a watch's `check` function). S1 audits both before any code change.

## Slices

- **S0:** the designSystem spec and preview for the four-group Work panel plus the rail. Josh approves. The held Activity cards (bd-ek2ye, bd-tn5g6, bd-0ohpa, bd-50ejc) are replaced by S0's build cards.
- **S1:** audit schedule, watch and `wait` (fields, triggers, delivery, persistence). Write the follow-up record shape and the migration.
- **S2:** console. Four groups on today's data, mapped client-side, with no backend dependency. Remove the activity section from Work.
- **S2b:** fold tasks into jobs. Migrate `task_*` records to jobs and keep `task_*` as aliases for one release.
- **S3:** D4 approvals. Server-recorded directives from decision answers, quote verification, and the scope-first display.
- **S4:** D2 follow-up primitive, plus aliases and the migration.
- **S5:** D5 prompt capability map and tool-count reduction. Remove the aliases one release after S4.

Independent fixes in flight that this ADR keeps: #5099 (a decision answer returns as soon as it is consumed) and projectBoard-plugin#735 (human page text, and pages retire when their card does).

## Decisions taken (Josh, 2026-11-11)

- **Group names:** Needs you · Doing now · Coming up · Your instructions. The rail chip for instructions reads "Told it N".
- **Not-started jobs** don't count in Doing now. They show under Coming up.
- **Interim decision link:** until D4's server-recorded approvals land, an instruction links to a decision when that decision was answered within the 2 minutes before the instruction was recorded.
- **The design** is the designSystem spec (docs/design/work-consolidation.md), approved 2026-11-11.

- **Q1, tasks:** folded into jobs. "tasks get folded into jobs."
- **Q2, approvals tied to a job:** close themselves when the job finishes. "anything tied to a finished job should close itself."
- **Q3, What happened:** not in Work. "what happened doesnt need to be in work at all, thats what we have telemetry and such for and that should already be tracked elsewhere."
