# Conversations in Work: original chats, private steering, retained history

Status: **proposal for team review; not implementation authorization.**
Prepared 2026-11-15, revised 2026-11-16 (America/Los_Angeles) after the
2026-11-16 steelman review (read against `main` at `d05f32262`) and the
2026-11-16 final planning audit, both source-reading only. Code re-inspected at
`a6ba26d10f3954bb21ee6c70c79f6373681c6bcb` (current `main`, release v0.246.1).
Backend findings below are verified by **source inspection only, not executed**;
runtime scenarios and cited tests were not run. A time-boxed Phase 0 isolation
prototype has since run separately (2026-11-16, board card bd-h6gka); its
executed-test evidence and **REVISE** verdict are summarized under
[Phase 0 outcome](#phase-0-outcome) below, which keeps the executed-versus-
inspection-only distinction explicit. Approval to review this document
is not permission to build, release, or create implementation cards. This change
adds this proposal and its changelog fragment; it leaves runtime behavior and
approved specifications intact.

## Decisions recorded 2026-11-16

Decided by Josh, 2026-11-16. These six answers are settled; the rest of this
document is written to be consistent with them, and every other question is
listed as open in the [decision map](#decision-map-settled-and-open).

1. **Who "the operator" is.** For private steering, "the operator" is the
   human's own console session only. Any agent, including one with operator
   rights (the fleet service token, paired agent devices), is treated as a peer.

   **Clarified by Josh, 2026-11-16.** "for this, are we including devices that
   I've authorized for myself, like my phone? that should be 'operator' as
   well". The human operator therefore includes the operator's own paired
   personal devices of kind `device` (a phone or browser), not only the console
   browser session; paired `agent` devices stay peers, matching the decision
   text above. [Section 3](#3-human-console-identity-and-delegation) carries the
   full definition and its limit, and the decision map's identity row records
   this clarification.
2. **Privacy contract.** Steering text and the turns it triggers are never
   delivered on peer channels (stream, poll, push, history, tool output). The
   agent may act on what it was told and deliver intended results. Relaying
   anything else needs an explicit operator instruction.
3. **Deleting a chat.** × on a chat tab still deletes the chat, as today. Work
   then shows that original as unavailable. The retention invariant is
   reworded: **no automatic retention sweep deletes a linked original while any
   linked work is active.**
4. **v1 scope.** The proposal carries a participant capability table; v1 covers
   the requester row plus originals stored on this host. Coder, reviewer and
   remote rows show as unavailable until their transcripts are durable. No
   copied fallback.
5. **ADR 0129 D8.** v1 keeps the structured decision controls and the
   missing-origin fallback. Removing them is a later, separate decision.
6. **What merging means.** Merging accepts the direction and the audience
   boundaries. It does not authorize building. Work starts on a separate go
   from Josh, beginning with a time-boxed Phase 0 isolation prototype that ends
   in continue-or-revise.

## Phase 0 outcome

**Verdict: REVISE (2026-11-16).** Per decision 6, Josh gave the go for Phase 0
only. The time-boxed isolation prototype on board card bd-h6gka ran one dispatch
round (no revise round used) against the real A2A seams at merge commit
`d669467b` and returned **REVISE**: the decision 2 privacy contract is **not yet
proven** while keeping one original conversation identity. A throwaway output
gate quarantined the tested **running-turn** steer output before the real
executor published it — preserving `context_id` and still delivering an explicit
intended result — but idle and parked/resume private inputs still enter SDK task
history before any output gate, audience scheduling and authorization are not
durable, and artifacts, tool output and checkpoint-derived content have bypass
paths. Nothing past Phase 0 is authorized; the findings below are prototype
evidence pending Josh's go, not approvals.

The executed-test evidence came from a **throwaway Phase 0 prototype**, separate
from this document's inspection-only findings table above. The focused run passed
**209 tests** across the five requested files
([test_steering.py](../../tests/test_steering.py),
[test_steer_idle_starts_turn.py](../../tests/test_steer_idle_starts_turn.py),
[test_a2a_hitl_parked_routing.py](../../tests/test_a2a_hitl_parked_routing.py),
[test_a2a_stores.py](../../tests/test_a2a_stores.py),
[test_chat_routes.py](../../tests/test_chat_routes.py)), including the new
experimental cases. As the report notes, passing characterization tests confirm
several leaks rather than proving privacy. The Phase 0 findings are filed as
GitHub issues and are **cited by number** here rather than restated.

**Chosen enforcement model: B (explicit deliver-to-requester), recommended.**
The prototype compared the two models the proposal names. Under **Model A** (the
agent labels each output's audience), an incorrectly peer-labelled private
response reached stream, subscription, poll, push and reopened SQLite history for
both fleet and paired-agent credentials — **executed**; labels alone cannot
enforce the contract against a wrong model decision. Under **Model B**, the same
test suppressed the private canary on all five tested surfaces and delivered the
intended result with the original context ID unchanged — **executed**. B is
recommended because ordinary output can be denied before publication and delivery
has a distinct enforcement point. It is **not** a complete solution: the
experimental action is an unauthenticated event, so recipient binding, scoped
operator-relay authorization and durable delivery records remain unimplemented
([#5400](https://github.com/protoLabsAI/protoAgent/issues/5400)).

Evidence summary by surface, **marked executed versus inspection-only exactly as
the report does**:

- **Executed** (real executor, SDK handler, SQLite store and push serializer;
  faked event source and HTTP receiver): a peer holding stream/subscription,
  GetTask/poll, push and durable-history handles receives the operator's
  running-turn steer and reply today
  ([#5400](https://github.com/protoLabsAI/protoAgent/issues/5400)); Model B
  blocks the **running sample** on stream and push and still delivers the result,
  but does **not** block GetTask/poll or durable history across idle and
  parked/resume states — those private inputs enter SDK history before any output
  gate ([#5401](https://github.com/protoLabsAI/protoAgent/issues/5401)); tool
  output and answer artifacts were exercised through the gate and are not
  generally covered.
- **Inspection-only** (not executed): gist, digest, harvest, facts, tracing,
  prompt capture and fleet trace export can each carry private steering content
  to a peer-reachable surface. The incognito suppression mechanisms are useful
  building blocks, but a private-turn-only flag is insufficient for a
  mixed-audience checkpoint, because a later public turn can clear it and
  downstream reads re-expose retained private material
  ([#5403](https://github.com/protoLabsAI/protoAgent/issues/5403)).

**Decision 1 identity mechanism and its limits.** The prototype added a
`classify_console(token, console_secret)` classifier: shared-operator,
fleet-service and paired-agent credentials classify as **peers**; a distinct
server-held console capability classifies as **human-console**; unknown
credentials fail classification. The executed identity test passed, confirming
operator rights are not equated with the console. **Limits:** the classifier is
not installed in HTTP middleware or routes; console credential issuance,
rotation, proxy propagation, revocation and loopback delegation are absent;
possessing a token cannot prove a human is using it; and protection against
agents that can reach console credentials, files or credential-management powers
is unproven ([#5402](https://github.com/protoLabsAI/protoAgent/issues/5402)).

**Two audiences in one context** was informed **negatively**: an executed test
showed a peer follow-up and a queued private steer both reach one model input, so
the proposed one-audience-per-turn queue rule remains unproven
([#5400](https://github.com/protoLabsAI/protoAgent/issues/5400),
[#5401](https://github.com/protoLabsAI/protoAgent/issues/5401)).

**Prototype location (throwaway; no PR, no push, no merge).** Worktree
`/Users/kj/dev/protoAgent-team/.worktrees/feat-bd-h6gka-phase-0-conversations-in-work-audience-i`,
branch `feat/bd-h6gka-phase-0-conversations-in-work-audience-i`, based on
`d669467be1ef02aa4a4440ee6e4bfbf6c6b6b072`. The prototype is
`src/a2a_impl/phase0_audience.py` with test additions kept in the working diff;
final evidence is `phase0-focused.log`. Exact commands as reported, in order
(the earlier commands iterated the `-k phase0` subset to green before the full
focused run):

```sh
.venv/bin/python -m pytest tests/test_a2a_hitl_parked_routing.py tests/test_a2a_stores.py tests/test_steering.py -k phase0 -q   # iterated to: 5 passed, 98 deselected
.venv/bin/python -m pytest tests/test_steering.py tests/test_steer_idle_starts_turn.py tests/test_a2a_hitl_parked_routing.py tests/test_a2a_stores.py tests/test_chat_routes.py -q   # 209 passed in 10.20s
```

The prototype's live PR-overlap refresh failed on GitHub connectivity, so
remote-main freshness at prototype time is unverified; the board overlap finding
in [Overlap check](#overlap-check) still stands from the 2026-11-16 audit. This
section records the Phase 0 outcome only; it creates no implementation cards and
authorizes no build.

## Design revision after Phase 0

**Proposed, pending Josh; design revision only (2026-11-16).** Josh authorized
“accept revise and go for design revision.” That accepts REVISE and authorizes
this amendment, not Phase 1 or any of Phases 2–6. The six decisions above remain
unchanged. This section replaces the earlier provisional mechanisms where they
conflict, without treating any remaining product question as settled.

### Evidence and recovery reference

The evidence baseline is the **bd-h6gka Phase 0 report**, verdict REVISE,
Model B recommended, 209 focused tests passed, recorded in
[Phase 0 outcome](#phase-0-outcome). The direct board deliverable was not
accessible in this editing session (no board tool was available); this is an
explicit provenance limitation, not a claim to have read that deliverable.
The four original issue bodies and the preserved prototype/test source and log
were read directly. No prototype tests were re-executed for this docs revision.

The named prototype worktree no longer exists. Recovery used local branch
`stranded/feat-bd-h6gka-phase-0-conversations-in-work-audience-i/20261116T233826.403Z`,
commit `8f21edbe2c3234a1f3bf8b9931c854736f481e85`, with `git show`, without
checking it out or copying code. At that commit, `src/a2a_impl/phase0_audience.py`
contains `quarantine` and `classify_console`; the Phase 0 cases are in
`tests/test_a2a_hitl_parked_routing.py`, `tests/test_a2a_stores.py` and
`tests/test_steering.py`. `phase0-focused.log` records **209 passed in 10.20s**.
This preserved local evidence is not a published implementation or an assertion
that the stranded branch was pushed. No external scratch checkout was created;
`board_record_work_location` was unavailable in this session.

All source references in this revision were rechecked against main at
`22f708fbd98ccffa5d29a5c43f5eba538f3b4ac6` (including merged bd-vh3p7,
[#5406](https://github.com/protoLabsAI/protoAgent/pull/5406)); remote main matched
that SHA when checked. Line numbers below refer to that baseline. Source
inspection is not execution evidence. In particular, #5403 remains entirely
inspection-only, and reopening SQLite in Phase 0 is not crash/restart proof.

### 1. Explicit deliver-to-requester action (Model B)

**Evidence.** The bd-h6gka report, [Phase 0 outcome](#phase-0-outcome), and
[#5400](https://github.com/protoLabsAI/protoAgent/issues/5400) report executed
`test_phase0_preexisting_handles`: Model A leaks when a model supplies the wrong
label; B suppresses the running canary and delivers the intended result across
stream, subscription, poll, push and reopened history. The prototype's B event
has no semantic authorization; it accepts arbitrary text as a deliverable.

**Proposed design.** Make delivery a server operation accepting a delivery-record
ID, never model-supplied destination or free-text bytes. At authenticated request
acceptance, bind the immutable requester principal, owning agent, original
`context_id`, request message ID and task ID. Display origin and `OPERATOR_OWNER`
are not requester identities. Shared credentials that cannot distinguish their
holders cannot establish a narrower recipient: refuse private release until a
stable device/service principal is enrolled. Delivery is only to that original
requester's authorized task handles and registered, revalidated push destination;
a second peer in the same context gains no right to it. Recipient changes require
a new server-side delivery record, not a model-edited URL, role or context field.

Per Q1, v1 uses **auto delivery**: the agent's intended result for the original
requester goes out through the deliver-to-requester action without a per-result
console approval step, exact-payload preview or release grant. The agent may act
on private instructions and deliver results. What the server still guarantees is
that the steer text and the agent's private replies to the operator are never
delivered on a peer channel (stream, subscription, poll, push, saved history,
tool output) and are never stored in peer-readable history. Auto delivery contains
the private exchange itself, not the model's wording, so it does **not** stop the
model from repeating what it was told inside a result. A “tell them the deadline
moved” relay is an ordinary delivered result under this action. Ordinary peer work
that has read no private material continues normally. A per-steer “hold for my
review” option is a possible later addition, not part of v1 (see Questions for Josh).

Persist a versioned delivery record in server-only columns on existing task
records (not wire-visible SDK metadata): delivery
ID, requester binding, private turn ID, release kind (result or relay),
payload/blob references and digest, credential generation, idempotency key, and
prepared/dispatching/delivered/canceled state. The delivery record and its sealed
payload commit before any publication. The dispatcher checks recipient, payload
digest and audience generation, then uses the same sealed payload for every
transport and durable peer projection. Changing one byte invalidates the record.
Cancellation prevents dispatch not yet committed; already published bytes cannot
be recalled. Retries reuse the delivery/artifact ID: history is idempotent; push
may be at-least-once and includes the stable ID. A crash after external send but
before acknowledgment must not invent a second release or claim exactly-once
network delivery.

**Seams.** [executor.py](../../src/a2a_impl/executor.py) `_flush_text` (1182),
`steer_consumed` (1479), `_finalize` (1207) and progress hooks must publish through
the release projection; [stores.py](../../src/a2a_impl/stores.py)
`ReasoningCoalescingTaskStore` (459) must persist releases and enforce recipient
reads before SDK serialization. Existing tier ownership remains an additional
check, not release authority.

**Rejected alternatives and risk.** Model A (the model labels each output's
audience), a bare unauthenticated B event, and a prompt asking the model for
discretion cannot enforce the contract against a wrong model decision; the
server-side deliver-to-requester action bound to a delivery record is the
enforcement point. Auto delivery (Q1) removes the per-result console step, so it
cannot stop the model from repeating private material inside a result it was
authorized to send: it contains the private exchange itself — the steer text,
private replies and their surfaces — not the model's wording. A per-steer "hold
for my review" option could add that check later; it is not part of v1.

### 2. Private storage for idle and parked steering

**Evidence.** The bd-h6gka report and
[#5401](https://github.com/protoLabsAI/protoAgent/issues/5401) executed
`test_phase0_output_gate_cannot_hide_private_input_history[False/True]`:
idle and parked/resume inputs reach polled SDK history before output filtering.
The prototype does not retain a durable console-readable private transcript.

**Proposed design.** Private steering and its replies **must not live in the
shared stored conversation**: neither SDK `Task.history`, shared artifacts,
shared checkpoint `messages`, nor their compaction archives. Store them in a
versioned console-only payload in server-only columns on existing task records,
separate from the SDK serialized task, plus private checkpoint state accessible
only to the trusted turn runner. Private checkpoint channels must never be
returned by a raw peer checkpoint/history API; the public runner receives a
projection with those channels absent. These are storage partitions of the same
original, not another
chat or summary-seeded fork. Keep one owning-agent/context identity, resolved
original thread ID, ordered turn IDs and task links; the console assembles that
original's authorized timeline from public and private partitions. Peers receive
only the public projection and their releases, with no private placeholders,
text, counts or private task IDs. Private continuation can read public context;
a peer model call can read only public context and released results.

Accept the private payload and deduplication record transactionally before an
idle start or resume. Loopback passes an opaque private-input reference and
scoped delegation, never private text as `SendMessage.parts`; intercept it before
SDK history acceptance, resolve it only inside the trusted runner. Parked input
uses the guarded task/interrupt resume path with the original checkpoint binding.
A peer-owned paused task remains the peer's; private resume computation and reply
stay in its private partition, while only an authorized result or content-free
state transition reaches its public task. Never put a private prompt into an SDK
`requires_input` message on a peer task. A missing private record, audience version
or resolver binding stops continuation; do not fall back to ordinary SendMessage.

**Seams.** [server/chat.py](../../src/server/chat.py) `_start_steer_turn` (883),
`_session_turn_idle` (964), `steer_or_start_turn` (1009), and
[hitl_routing.py](../../src/a2a_impl/hitl_routing.py) need private intake before
SDK persistence; [stores.py](../../src/a2a_impl/stores.py) `save/get/list` and
session history readers must project by console identity, not operator tier.
[thread_ids.py](../../src/graph/thread_ids.py) resolution metadata must persist
with the original rather than be reconstructed from the idle call's empty metadata.

**Rejected alternatives and risk.** Post-save filtering cannot undo a leaked
input. Labelling every message inside the shared SDK blob leaves every generic
reader one missed filter away from a leak; a side chat loses decision 1's original
conversation context. Partitioning requires schema-version checks on all readers,
including rollback binaries; unsupported readers must refuse protected records.
Decision 3 applies to both partitions: × deletes the original, including private
payloads/checkpoints, and Work shows unavailable; automatic sweeps keep the whole
original while linked work is active. An incomplete deletion remains unavailable
and retries cleanup, never restores the private partition to shared history.

### 3. Human-console identity and delegation

**Evidence.** The bd-h6gka report and
[#5402](https://github.com/protoLabsAI/protoAgent/issues/5402) executed
`test_phase0_identity_does_not_equate_operator_rights_with_console` and the
operator-tier fleet/paired variants of `test_phase0_preexisting_handles`.
The prototype classifier is not installed in HTTP middleware and has no
credential lifecycle; no transport identity has been proven.

**Proposed design.** The distinction decision 1 needs already exists in the
credentials and does **not** require a new console-only credential. Per Josh's
2026-11-16 clarification, **the human operator is the console's shared operator
bearer plus the operator's own paired devices of kind `device`** (phones and
browsers). **Peers are the fleet service token, paired devices of kind `agent`,
and the federation token.** The seam is that `_classify_token`
([auth.py](../../src/a2a_impl/auth.py) line 481) gives every paired device the
**operator** tier — a phone resolves to scope `device`, an agent device to
`full` (lines 506–515) — and the shared operator bearer and fleet service token
also resolve to operator (lines 496–505), while the federation token is its own
`federation` tier (lines 498–500). The trust tier therefore cannot tell the
human's phone from an agent's hub; the **device kind can**. That kind is fixed
when the pairing code is minted and copied onto the device at claim time
([devices.py](../../src/security/devices.py) lines 27–28 and 131–133), "never
from anything the claimer sent", so a claimer cannot relabel an `agent` code as
`device`. Propagate a verified **principal kind** (shared-bearer, `device`-kind
device, fleet-service, `agent`-kind device, federation) from `_classify_token`
and the device record, alongside and separate from `trust_tier`, and gate
private reads, sends and release grants on principal kind, not on tier.

Issuance, rotation and revocation reuse the **existing device lifecycle** rather
than a new one: a human's device is enrolled by an operator-authed pairing with a
`device`-kind code (ADR 0087); tokens rotate with reuse-detection grace (ADR 0121
D5, `prev_token_sha256`/`rotated_at`); removal or a rotation-reuse hit revokes the
device; the shared operator bearer rotates via the existing wizard/drawer path.
Keep the bearer and pairing secrets out of model prompts, workspaces, tool
environments and agent-readable config, and keep storing only token hashes
server-side, as devices.py already does. No new server-held console secret,
HttpOnly session cookie or enrollment ceremony is proposed for the paired-phone
case; hardening the shared bearer's transport (cookie, lifetime, liveness
recheck) is a separate, optional security change that this definition does not
depend on, and nothing here is proposed beyond this definition.

The fleet proxy must strip untrusted principal headers, authenticate the caller
and mint a short-lived, signed delegation for the specific member, method,
context and operation. The owner verifies issuer, target audience, expiry, nonce
and revocation generation; a swapped fleet bearer alone remains a peer — fleet is
a peer kind whatever operator tier it earns. Private access fails closed if
revocation cannot be checked. Loopback uses a single-operation delegation bound
to the persisted private-input ID and task, redeemed idempotently; localhost and
`self_invocation_headers` do not confer human-operator status. Ordinary
self-invocations keep their existing powers but cannot read private partitions or
mint release grants.

**Seams.** [auth.py](../../src/a2a_impl/auth.py) `_classify_token` (481), request
classification, SSE identity and `self_invocation_headers` (1145);
[devices.py](../../src/security/devices.py) `kind`/`effective_scope` (153);
[stores.py](../../src/a2a_impl/stores.py) `tier_context_builder` (445);
[fleet/proxy.py](../../src/graph/fleet/proxy.py) credential swapping; and
[server/chat.py](../../src/server/chat.py) loopback intake. Propagate verified
principal kind and capability separately from `trust_tier`. The shared operator
bearer and paired `device`-kind devices are the human operator; the fleet service
token, paired `agent`-kind devices and the federation token always remain peers
for private content (decision 1, as clarified by Josh 2026-11-16).

**Rejected alternatives and risk.** A new console-only credential or enrollment
ceremony for the paired-phone case is rejected: the device kind already carries an
unforgeable (claimer-cannot-relabel) human-versus-agent distinction, so a second
secret only duplicates the pairing trust root — and the prototype's standalone
`classify_console` secret was never installed in middleware. Operator tier, a
browser user-agent string, loopback origin and a client-set audience header prove
no human identity. The device kind proves a credential was **paired for the
operator's personal device use, not that a human is present, attentive or
unautomated**, and not *who* holds it. Agents with access to the human's browser,
bearer, credential files or credential-management powers can impersonate it.
Per Q2, private steering stays enabled everywhere, including where those resources
cannot be isolated from agents; a classifier cannot solve same-OS-user compromise,
and that risk is accepted. The kind-records-pairing-not-user limit is raised under
Questions for Josh; it is not a reason to relabel an agent as human or reopen
decision 1.

### 4. One audience per turn, durably scheduled

**Evidence.** The bd-h6gka report and
[#5400](https://github.com/protoLabsAI/protoAgent/issues/5400),
[#5401](https://github.com/protoLabsAI/protoAgent/issues/5401) provide executed
negative evidence: `test_phase0_running_steer_mixes_peer_and_private_input`
puts both inputs into one model call. There is no executed durable scheduler or
queued-input crash recovery in Phase 0.

**Proposed design.** Use one serialized runner per resolved original thread.
Persist accepted inputs as pending turns on existing task records, with monotonic
sequence, audience, verified principal, message-id deduplication key, checkpoint
binding, interrupt ID and queued/running/parked/terminal state. Do not introduce
a second work scheduler or binding database. Assign audience before the first
model/tool call; it cannot change mid-turn. A private steer arriving during a
peer turn queues. At the next safe model/tool boundary the peer execution yields,
commits its public checkpoint and closes its execution segment; a distinct
private turn can then run on the same original. Already-started tool effects
cannot be undone or retroactively made private. Peer inputs arriving while a
private turn runs queue; middleware must never drain them into that private call. Historical public context remains readable by a private turn;
“one audience” governs newly accepted inputs and all output of that execution,
not a ban on reading the original public request.

Order new turns by durable acceptance sequence, with no cross-audience batching.
A running segment reaches its safe boundary first; a parked segment reserves the
runner until its matching guarded resume or explicit cancellation. A matching
resume is continuation of that segment, not a later queue item; it may pass
queued fresh inputs solely to unblock that interrupt. If a human's private
answer resumes a peer segment, close the suspended public segment at the
interrupt and persist a private successor segment on the same task/checkpoint
binding before model execution. Its outputs follow sections 1–2. This avoids a
mixed-audience call without bypassing D8's guarded decision controls. Ordinary
steers do not satisfy arbitrary parked decisions. A long park can delay peers;
expose a content-free waiting state and let the operator cancel/resume explicitly.

Use transactional compare-and-set on the active segment/generation, not only a
process-local lock. Accept-and-queue wins or completion-and-next-start wins as
one ordering decision. Recovery reloads the durable queue and audience before
claiming a runner; expired execution leases do not replay a tool effect or
release blindly. Unknown execution outcome stays suspended for reconciliation.
Persist private payload/checkpoint references before advancing the queue; missing
records leave a recoverable private error, never a public restart.

**Seams.** [server/chat.py](../../src/server/chat.py) idle/start and
`restart_undrained_steers` (1076), [steering.py](../../src/graph/steering.py)
in-memory queue, [middleware/steering.py](../../src/graph/middleware/steering.py)
`_drain` (85), HITL routing and the executor's guaranteed `turn_ended` hook.
The queue becomes a projection of durable task state; existing locks remain useful
for local coordination but are insufficient for restart correctness.

**Rejected alternatives and risk.** Draining both audiences and hiding the reply
fails at model input; toggling an audience flag after streaming has begun is too
late. FIFO without a matching-resume exception deadlocks a park. The proposed
safe boundary may delay steering while a tool runs and parked turns may starve
peers; neither warrants silently mixing audiences. These timing semantics need
Josh's review, not an assertion that the Phase 0 scheduler passed.

### 5. Derived readers across later turns

**Evidence.** The bd-h6gka report and
[#5403](https://github.com/protoLabsAI/protoAgent/issues/5403) are **source
inspection only** for every surface below. A later ordinary turn can clear
`incognito` while older private messages remain in its checkpoint. No executed
claim for gist, digest, harvest, facts or telemetry follows from the 209 passes.

**Proposed design.** Private partitions never enter the shared checkpoint's
`messages` or archives. Record an immutable audience and source-version on each
partition and derived record; route all readers through an explicit public or
console-only projection. A private execution can read the original's public
checkpoint plus private state, but its computed state, summaries, tool results
and compactions remain private. A later peer turn uses the same original
checkpoint identity's public projection and only authorized deliveries. It never
loads private state and then tries to redact the finished answer. This is not a
latest-turn flag: protection follows retained data and its derivations.

| Reader and current seam | Proposed exclusion, including the next ordinary turn |
| --- | --- |
| Gist — [session_gist.py](../../src/graph/session_gist.py) `schedule` (63), `_generate` (123) | Give the writer only public messages and a public previous gist. Bind delayed jobs to the source projection/version; recheck before committing, so a stale timer cannot restore a contaminated gist. Private transcript is retained for console viewing, with no automatic private gist in v1. |
| Digest — [middleware/memory.py](../../src/graph/middleware/memory.py) `_persist_session`, `digest_entry` (512), `load_digest_pool` (666), closed/relevant loaders | Compute topics, counts, final outputs and tool summaries only from public projection; filter open, closed and relevant-search cards by source version/audience on read as well as write. Suppressing a new write must not preserve an old private gist. |
| Harvest — [conversation_harvest.py](../../src/graph/conversation_harvest.py) `archive_payload` (119), `harvest_thread` (748–858) | Read public checkpoint messages and public archive spans together; never concatenate private archives into a public summary. Persist audience/source version on summaries and closed-chat cards. Private archive retention remains in the private partition, not searchable shared history. |
| Facts — [conversation_harvest.py](../../src/graph/conversation_harvest.py) `facts_allowed` (704), extraction (864–882) | Retain existing eligibility checks and additionally pass only public input to extraction. Carry public source lineage into fact storage, deduplication and recall; no private fact may merge into or supersede a shared fact. Disable extraction for unknown/mixed provenance. |
| Tracing — [tracing.py](../../src/observability/tracing.py) `io_allowed` (1018) | Suppress all content for any call that reads private state, including prompts, arguments, results, errors and nested/background spans. Allow only bounded content-free operational metrics. A later public call is traceable only after its complete composed input, including memory, is verified public. |
| Prompt capture — [prompt_capture.py](../../src/graph/middleware/prompt_capture.py) `_capture` (121) | Apply the same private-input suppression before composed/wire prompt capture and response capture; drop private buffers, not just the final disk write. A later turn cannot restore private snapshots via a shared capture cache. |
| Fleet trace export — [trace_export.py](../../src/observability/trace_export.py) `export_turn` (260) | Export only verified public checkpoint projection and public auxiliary state (including plan/tool schema/metadata). Skip the whole export if lineage is missing or mixed; never trust only the latest `incognito` value. Bound and redact diagnostics; fleet credentials still do not read private state. |

Before enabling private steering, mark already-derived records from a migrated
mixed/unknown source unavailable for peer recall and regenerate from verifiable
public input. Include encoded/raw card paths, closed cards, search indexes,
compaction archives and cached prompts. Do not guess that an old summary is safe
because its text lacks the canary. A projection/source-version mismatch or read
failure omits the derived item and schedules reconciliation; it never falls back
to the raw checkpoint. Data already exported cannot be recalled by migration.

**Rejected alternatives and risk.** Reusing only incognito, dropping just private
input messages, or fixing writers without readers leaves later-turn, assistant
paraphrase and stale-cache leaks. Permanent incognito for the entire original
would suppress useful public memory without separating retained transcripts.
Partitioning plus lineage is more invasive and could omit useful public context
when provenance is missing; fail-closed omission is preferable to an unsupported
privacy promise. Custom checkpoint/archival plugins must advertise this contract
or private steering remains unavailable for them.

### 6. Files, structured output, tools, errors and cancellation

**Evidence.** The bd-h6gka report and
[#5400](https://github.com/protoLabsAI/protoAgent/issues/5400) executed tool-end
and answer events through the prototype gate. The file/structured finalization
bypass and arbitrary tool side effects are **inspection findings**, not executed
proof of comprehensive quarantine. #5403's observation paths remain relevant to
terminal/error hooks as well.

**Proposed design.** Apply the delivery record to the final serialized envelope,
not just text events. Private files are staged outside peer-readable workspace
paths; bind hashes, names, MIME types, sizes and file versions to a delivery
record. Downloads and signed URLs must check the intended principal and live
delivery record, not merely possession of an old task or broad operator credential.
A preview or thumbnail is also content. Park/resume carries private file ownership
and audience; no context-only carry may attach them to the next peer answer.
Structured output is private until its entire DataPart and metadata are sealed into
a delivery record. Finalization cannot append fields/files after that seal.

Tools inherit the turn's audience and an effect policy before invocation. Local
private computation can run in isolated scratch storage. Per decision 2 the agent
may act on what it was told, so publishing tools (A2A/send, email, web requests
containing data, shared file writes, commands, plugins and delegated agents) no
longer need a console-authorized destination-and-exact-payload/effect manifest:
the agent may invoke them to carry out the instruction. **Unknown or unsandboxable
effectful tools are unavailable in private turns** (unchanged). A delegate is a
peer under decision 1: delegating the steer content itself is a relay to that peer
and rides the same server-side delivery record, not a private side channel. Tool
arguments, output, progress, stdout, errors and files from a private segment stay
private unless they are part of the delivered result; carrying out a side effect
does not by itself publish its arguments or output. The practical limit is
enforcement at filesystem/network and tool boundaries, not a model-generated
“safe tool” label.

All errors, stalls, implicit end-of-stream, cancellation and cleanup paths use
the same audience projection. Peers get fixed content-free status codes; raw
exceptions, partial private answers, paths and provider bodies stay private.
Cancellation durably prevents uncommitted delivery/effects, drops private file
claims and settles the queue without flushing buffers onto peer handles. Effects
already committed remain recorded and cannot be undone by Stop; an unknown
external-send outcome requires reconciliation, not blind replay. Authorized
public output emitted before private acceptance is not retroactively secret.

**Seams.** [executor.py](../../src/a2a_impl/executor.py) `_append_structured`
(906), `_finalize` (1243–1267), parked file helpers (585–716), tool event branches,
error/finally paths (1604–1688) and `cancel` (1692). Extend enforcement at both
tool-call wrappers in [subagent_fence.py](../../src/graph/middleware/subagent_fence.py)
(199–224), preserving existing fences. File checks include
[infra/media.py](../../src/infra/media.py) `check_access` (237), reminting and
parked ownership (681–740), plus [server/media.py](../../src/server/media.py)
GET/HEAD, open-link and remint routes (37–80). Private resources must not inherit
the public-media or signature-only bypass. Terminal progress hooks and tracing
are also exits, even when no answer artifact was emitted.

**Rejected alternatives and risk.** Filtering only `text`/`done`, stripping an
error after it was logged, or granting all tools operator rights leaves exits
outside Model B. Hashing a mutable file path is not binding its delivered bytes.
The risk is reduced private-turn tool capability and unresolved side effects
across crashes; unsupported effects must remain blocked rather than pretending
that output filtering contained them.

### Questions for Josh

These are trade-offs for the proposed mechanisms, not amendments to the six
recorded decisions. Josh gave his answers on 2026-11-16; each is
recorded verbatim under its question below. Q3, Q4 and Q5 are decided
(2026-11-16); Q1 and Q2 are decided (2026-11-16). None changes the six recorded
decisions or the decision-1 clarification.

- **Result release interaction:** the unauthenticated B action in #5400 can carry
  any text. The question was whether to require an exact-payload console release
  for every private-derived result — enforceable, but a step even for intended
  results — or to adopt auto delivery through the explicit deliver-to-requester
  action. Decision 2 remains unchanged either way.

  **Decided by Josh, 2026-11-16.** Before the choice was explained: "i think auto
  delviery for releasing results to a peer. im not sure the exact problem that is
  trying to solve tbh, help me understand". After the explanation: "yes auto
  delivery, and yes keep it on everywhere".

  v1 uses **auto delivery**: the agent's result for the original requester goes out
  through the explicit deliver-to-requester action (Model B). There is **no console
  grant, no exact-payload release and no preview.** The server guarantees that
  steering text and the agent's private replies to the operator are never delivered
  on peer channels (stream, subscription, poll, push, saved history, tool output)
  and are never stored in peer-readable history. This does **not** guarantee that
  the model won't repeat what it was told inside a result: auto delivery contains
  the private exchange itself, not the model's choice of words. A per-steer "hold
  for my review" option is a possible later addition and is not part of v1.
  Decision 2 remains unchanged.
- **Deployment boundary:** #5402 cannot distinguish a human from an agent that
  steals console authority. Accept disabling private steering where credential
  and tool isolation cannot be established, rather than weakening decision 1?

  **Decided by Josh, 2026-11-16.** On 2026-11-16: "accept the risk, im not too
  stressed about this situation happening." Confirmed 2026-11-16: "yes keep it on
  everywhere".

  Private steering stays **enabled everywhere**; it is not disabled where
  credential and tool isolation cannot be established. The risk that an agent
  obtains console authority is accepted. There is **no per-deployment disable
  switch.** This is the opposite of the option the question proposed (which was to
  disable steering in that case). Decision 1 is unchanged.
- **Device kind records pairing, not who uses it:** section 3 treats the shared
  operator bearer and paired `device`-kind devices as the human operator. A
  device's kind is set at pairing time (src/security/devices.py; fixed when the
  code is minted, so the claimer cannot relabel it) and records *how* a
  credential was paired, not *who* uses it. An agent paired with a `device`-kind
  code — an automation or recorder handed a phone-style pairing — or any agent
  holding the shared operator bearer, would be treated as the human operator.
  Accept this for v1 (operator-authed pairing and bearer custody are the trust
  boundary), or require a stronger per-human proof before private release? This
  is a limit to weigh, not a decision taken here; decision 1 is unchanged.

  **Decided by Josh, 2026-11-16.** "yes anything paired as a phone style device."
  For v1, anything paired as kind `device` counts as the human operator; no
  stronger per-human proof is required. The pairing-versus-user limit above
  stands and decision 1 is unchanged.
- **Steering latency:** #5400's mixed-input counterexample motivates a new private
  execution segment at a safe boundary; parked segments reserve the runner. Is
  delayed steering/queued peer work acceptable for v1? No implicit interrupt
  bypass or change to decision 5 is proposed.

  **Decided by Josh, 2026-11-16.** "yes, thats how it works for all of our other
  steering afaik." Private steering waits for a safe boundary and peer work queues
  behind it. No implicit interrupt bypass or change to decision 5.
- **ADR home:** recommend an ADR 0131 amendment describing storage partitions
  and audience scheduling on existing records, rather than a parallel lifecycle
  store.

  **Decided by Josh, 2026-11-16.** "as recommended." Record the supersession as an
  amendment to ADR 0131, not a new ADR. This card writes no amendment and edits no
  ADR file; the amendment belongs with Phase 1, after Josh gives a go.

## Phase 1 as revised

**P1a–P1g below are authorized proof slices (Josh, 2026-11-16); P1h stays
proposed, needs Josh's go.** On 2026-11-16 Josh said "then we are ready for
phase 1", authorizing the bounded proof slices. These are small board slices, not
permission to go past them. The old Phase 1 row is refined by these gates; Phases
2–6 remain unauthorized. Start only with the bounded proof slices needed to
execute the next section; production integration, migration and UI enablement
(P1h) wait for the proof gate's evidence and Josh's CONTINUE decision. A green run
alone does not authorize further phases.

| Slice | Concrete scope and dependency | Status |
| --- | --- | --- |
| P1a identity proof | Propagate a verified principal kind from `_classify_token`/the device record, separate from the operator tier: the shared bearer and `device`-kind devices read as the human operator; the fleet service token, `agent`-kind devices and federation read as peers. Scoped loopback; reuse existing device pairing/rotation/revoke, **no new console credential**, no private UI. Prove fleet-token and `agent`-device rejection and `device`-kind acceptance before private reads. | authorized: proof slice (Josh, 2026-11-16) |
| P1b private persistence proof | Private input/reply partition and public history projection on existing task records; idle and guarded parked resume, with original resolver binding. Depends on P1a. | authorized: proof slice (Josh, 2026-11-16) |
| P1c scheduling proof | Durable sequence/segment claim, safe boundaries, deduplication, park/resume and crash recovery on the existing runner. Depends on P1b. | authorized: proof slice (Josh, 2026-11-16) |
| P1d delivery proof | Auto delivery to the original requester, requester binding, recipient projection and durable delivery state. Depends on P1a–c. | authorized: proof slice (Josh, 2026-11-16) |
| P1e other-exit proof | Seal structured/files at finalization; private tool/effect restrictions, error/cancel cleanup and scoped downloads. Depends on P1d. | authorized: proof slice (Josh, 2026-11-16) |
| P1f derived-memory proof | Public projection for gist/digest, then harvest/archives/facts in a separate small slice; test later ordinary turns and stale records. Depends on P1b–c. | authorized: proof slice (Josh, 2026-11-16) |
| P1g observability proof | Trace/prompt suppression and public-only fleet export, including background/terminal paths and later turns. Depends on P1b–c. | authorized: proof slice (Josh, 2026-11-16) |
| P1h integration, only after proof gate | Reviewed schema migration/reconciliation, compatibility refusal, rollback gating and operator diagnostics. Release UI requires its own approved spec/preview before boarding; no Work composer yet. Waits for the proof gate below and Josh's CONTINUE. | proposed, needs Josh's go |

## Proof that turns REVISE into CONTINUE

**Required future execution, not a claim of passes today.** Keep the existing
209-test Phase 0 result as historical characterization. Before Phase 1 work
beyond the bounded proof slices starts, execute the following adversarial matrix
against real intake, SDK handler, executor, persisted stores and serializers.
Use deterministic model/HTTP fakes where appropriate, but assert at received
peer surfaces and reopened stores, not merely a mocked audience helper. Record
command, commit, exact scenario IDs, surface assertions and limits in the next
report. Run each identity-sensitive scenario for federation, fleet-service,
paired-agent and enrolled-console callers; no operator-tier shortcut.

| Required scenario | Surface and pass condition; existing test seam |
| --- | --- |
| Running with preexisting handles | Extend `test_phase0_preexisting_handles` in `tests/test_a2a_hitl_parked_routing.py`: private input/reply/tool canaries absent from stream, subscription, poll, push and reopened history; exact authorized result present once in durable projection with unchanged context. Also check console progress hooks, history and private recovery. |
| Wrong recipient / overbroad release | Forge requester, context or task IDs, replace the push destination and add a second peer to the context. Nothing is delivered to anyone but the original requester: neither a wrong label nor a raw B event releases to a forged, substituted or second recipient, and the original requester receives only the sealed payload. In the same run, private input and the agent's private replies never appear on any peer surface — stream, subscription, poll, push, saved history or tool output. |
| Idle and parked/resume | Replace the leak assertions in `test_phase0_output_gate_cannot_hide_private_input_history[False/True]` with privacy assertions at intake and every peer read, not only output. Prove console recovery of input and reply, task/interrupt binding, private files and a subsequent peer follow-up. Preserve #5380 ownership. |
| Both queue orders and completion race | Extend `tests/test_steering.py` and `tests/test_steer_idle_starts_turn.py`: peer then private, private then peer, steer exactly as completion commits, duplicate POST, failed loopback/requeue, matching resume behind queued input, and a long-running tool. Inspect actual model inputs: no mixed audience. One durable turn per message ID in acceptance order. |
| Actual process restart | Kill/restart the runner after accepted input, during claimed execution, while parked, after delivery-record commit and after external send before acknowledgment. Reopen checkpoint and task stores; recover original resolver metadata, audience, queue order and private transcript. No duplicate effect, unauthorized send or fallback public input. Reopened SQLite alone is insufficient. |
| Identity lifecycle | Extend `tests/test_a2a_stores.py` and `tests/test_chat_routes.py` with authenticated HTTP/SSE and proxy/loopback integration: `device`-kind pairing issuance, device token rotation (reuse-detection grace), logout/revoke, expiry, missing revocation service, forged delegation, wrong target, replay, and a claimer attempting to relabel an `agent` code as `device`. A shared operator bearer and a `device`-kind device read as the human operator; the fleet service token, `agent`-kind devices and federation cannot mint operator-console authority or read private history. |
| Every derived reader on a later ordinary turn | Execute separate cases for gist, digest (open/closed/relevant), harvest, facts, tracing, prompt capture and fleet export. Seed private canaries in retained private messages/replies/tool output and legacy mixed archives/cards; run a public turn with `incognito=False`, compaction and restart. Assert canaries absent from derived stores, search/recall, composed peer prompts and actual exported bytes; stale gist timers and unknown provenance cannot restore them. These tests supply the currently missing #5403 execution evidence. |
| Other exits and termination | Inject private values into structured DataParts, file contents/names/URLs, tool args/output, outbound tool effects, errors, stalls, implicit stream end and cancellation. Capture final serialized artifacts, download responses, side-effect requests and terminal hooks. Deny exits that are not part of a delivered result, preserve delivered results, and ensure Stop never flushes private data or restarts canceled input. |
| Missing/stale state and deletion | Missing private record/checkpoint, conflicting audience version and unsupported resolver fail closed. × deletes both partitions and leaves Work unavailable; stale derived readers cannot resurrect them. Automatic sweep and recovery do not release active-work retention references. |

CONTINUE requires all applicable privacy assertions to pass and an explicit
review of unsupported tools/deployments and residual risks; no generic “209
passed” substitution. A failure keeps REVISE. Josh has authorized the bounded
proof slices (P1a–P1g, 2026-11-16); this revision does not execute these tests,
and **CONTINUE past the proof slices stays Josh's decision**, taken on the
evidence this matrix produces.

## Decision sources and approval boundaries

The operator approved the direction in the proposal request: put progress and
related conversations in Work, remove the standalone From others tab, open the
original conversation, keep one conversation action per row, keep steering and
its private responses operator-only, and retain original history while linked
work is active. The agent may still deliver intended work results to peers.
The six decisions above refine that direction; decisions 1 and 2 define what
"operator-only" and "private" mean precisely.

The supplied design references are **spec `a-1794812909598-cd8b26` v2** and
**preview `a-1794812482334-68d817` v4**. These are artifact references, not durable
repository files. Their substantive design is captured below; review does not
depend on an expiring preview URL. Visual sign-off does not establish backend
readiness, authorization, privacy, retention, or actual-device acceptance.

| Evidence category | What it establishes |
| --- | --- |
| Operator-approved direction | Work-to-original-Chat navigation; one Open in chat action per available related conversation, including Needs you; private steering and private responses; active-work history retention. Refined by the six decisions of 2026-11-16 above. |
| Existing accepted design | [ADR 0129](../adr/0129-work-consolidation.md) defines four Work groups, job identity and execution ownership, recovery, and answer-in-place. [ADR 0131](../adr/0131-work-knows-its-conversation.md) commits P1/P2, extends existing stores, and treats origin as provenance, not authority. Its P3–P5 remain subject to later decisions. |
| Existing layout approval | [work-view.md](./work-view.md) supplies row, rail, inspector, phone, focus and failure rules. [work-consolidation.md](./work-consolidation.md) replaces its old group structure with Needs you, Doing now, Coming up and Your instructions. |
| Historical conversation approval | [other-conversations.md](./other-conversations.md), revisions 1–5, describes the pinned read-only From others tab, Continue in a new chat, and links between threads and jobs. That approval remains recorded; this proposal explicitly replaces that interaction rather than claiming it meets the new contract. |
| Newer accepted architecture | [ADR 0130](../adr/0130-server-authoritative-live-sync.md) makes server state and resync authoritative. [ADR 0132](../adr/0132-chat-context-carryover.md) covers memory, gists, digests and checkpoint recovery, not full-history preservation or private peer conversations. [ADR 0133](../adr/0133-model-roles-with-fallback-chains.md) covers model chains, not conversation audiences. |
| Review evidence on record | The 2026-11-16 steelman review confirmed the code claims in the findings table against `main` at `d05f32262` and added the operator-tier, ownership, ×-delete and v1-scope findings folded in below. The 2026-11-16 final planning audit added the participant-capability, derived-content, observability, read-only-milestone, prototype and two-audience gaps, and verified the live board. Both are inspection-only. |
| Preview proposals, not separately confirmed decisions | Routine threads in a Started by others group in the Chat list; the preview's completed-work presentation; unknown output audience defaulting to private. These remain open in the decision map. |

### Lineage and supersession

This proposal **supersedes** the interaction design of From others revision 5
([#5339](https://github.com/protoLabsAI/protoAgent/pull/5339)) — the pinned
read-only tab — and the shipped Continue in a new chat flow (C4,
[#5301](https://github.com/protoLabsAI/protoAgent/pull/5301)) together with its
guide ([#5353](https://github.com/protoLabsAI/protoAgent/pull/5353)). It
**builds on** ADR 0131 P2c (`GET /api/conversations` rows list their open
tasks, [#5329](https://github.com/protoLabsAI/protoAgent/pull/5329)), the Work
"Started in" provenance line
([#5342](https://github.com/protoLabsAI/protoAgent/pull/5342)), and board cards
recording the conversation that caused them (bd-edo5o,
[projectBoard-plugin #777](https://github.com/protoLabsAI/projectBoard-plugin/pull/777),
merged). Whether the supersession is recorded as an ADR 0131 amendment or a new
ADR is open (settled in Phase 0). The historical approval of revision 5 stays
recorded; nothing here claims the old behavior meets the new contract.

### ADR 0129 D8 boundary

Visual approval of a Needs you row does not silently amend ADR 0129 D8's
answer-in-place contract. Decision 5 settles the v1 boundary: **v1 keeps D8's
structured decision controls (Reply / Done / Dismiss) and the missing-origin
fallback.** Removing them is a later, separate decision, taken in its own
review. The approved new flow adds no Work conversation composer; the single
Open in chat action replaces conversation-opening controls, not D8's decision
controls.

## The proposed experience

Work answers what is happening and which conversations explain it. Main Chat is
where the human operator reads and steers the owning agent. No fork, copied
history, summary-seeded new chat, or new Work composer satisfies this proposal.

### Work list, requests and promotion

Keep the four Work groups and their shared counts. Actionable incoming requests
may appear as Work items before a task, job or board card exists. Show the request,
its state, and **Asked by** with the original requester and known provenance.
An automated conversation alone does not establish that there is actionable work;
request eligibility remains an open product decision (settled in Phase 0).

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
the decision 2 contract: steering from the human operator's console, and the
turns it triggers, are never delivered on peer channels; the agent may act on
what it was told and deliver intended results. This promise must not ship until
the audience boundary is proven. Sending a new turn within the same original
context is allowed; minting a replacement conversation is not. A parked decision
must use its matching guarded resume path, not a new prompt that bypasses the
interrupt.

### Participant capability table and v1 scope

Not every related conversation has an original that main Chat can open. The
rows below ground decision 4's v1 scope. "Unavailable" rows still show role,
participant, state and provenance; they have no Open action and never fall back
to a copied history.

| Participant type | Example row | Durable original on this host | Full history | Steerable | Private steering possible | v1 behavior |
| --- | --- | --- | --- | --- | --- | --- |
| Local A2A requester (session on this host) | Requester · Claude Code for Josh | Yes — A2A task store turns | Yes, subject to retention | After audience isolation ships | To be proven in Phase 0/1 | **Open in chat** |
| This agent's own chat (operator-started) | Your instruction | Yes | Yes | Already today's chat | Not applicable (no peer) | **Open in chat** |
| Remote A2A thread (owned by another agent's host) | Requester · remote fleet member | No — lives on the owner's host | No | No | Unproven; needs owner-side guarantees | Unavailable |
| ACP coder | Coder · Codex | No — the adapter keeps only a `transcript_tail` ([acp_adapter.py](../../plugins/delegates/acp_adapter.py), line 380), in process | No | No | No | Unavailable |
| Board coder | Coder · Claude | No — evidence is the board card and PR, not an A2A session | No | No | No | Unavailable; board/PR links stay as progress evidence |
| Plugin reviewer | Reviewer · Vera | No — runs as a plugin, not a persisted conversation | No | No | No | Unavailable |
| Discord requester | Requester · Discord thread | No — the original is a public Discord thread | Discord-side only | Replying there is public | **Never** — the channel is public | Unavailable for Open in chat; Asked by and provenance still shown |

Decision 4: v1 covers the requester row plus originals stored on this host.
Coder, reviewer and remote rows show as unavailable until their transcripts are
durable. No copied fallback. Upgrading any row from unavailable requires the
capability to be real first (durable transcript, owner routing, audience
guarantee), then a reviewed change — not a UI toggle.

### What this replaces in today's console

There is no current control labelled "Answer in chat"; that label never
shipped. Today's controls, named so the replacement is reviewable:

- **Open conversation** in the Needs you rail section
  ([NeedsYouSection.tsx](../../apps/web/src/chat/NeedsYouSection.tsx), line 119);
  for non-chat threads it opens the read-only reader (C6b).
- **Continue in a new chat** in the reader
  ([ConversationReader.tsx](../../apps/web/src/chat/ConversationReader.tsx),
  line 290), backed by `chatStore.continueConversation`
  ([chat-store.ts](../../apps/web/src/chat/chat-store.ts), line 1613), which
  creates a new session and seeds a draft.
- The D8 **Reply** form and the **Started in chat …** provenance line
  ([tasks.tsx](../../apps/web/src/work/sections/tasks.tsx), lines 230 and 180).

The proposal replaces the first two with one **Open in chat** action per
available row. Per decision 5, the D8 structured controls and missing-origin
fallback stay in v1. "Started in" remains as provenance.

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
| Original no longer stored | Show an unavailable row with no Open action and explain the known loss. No copied fallback. Covers lost, unavailable and remote data, and a chat the operator deleted with × (decision 3): the delete still happens as today, and Work then shows the original as unavailable. |
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

All findings here are **inspection-only, unexecuted**, re-verified at
`a6ba26d10f3954bb21ee6c70c79f6373681c6bcb` (current `main`, v0.246.1). The
2026-11-16 steelman review independently confirmed the earlier version of this
table against `main` at `d05f32262`; rows added since then come from that review
and the final planning audit, re-checked here. Line numbers are as of this
inspection and will drift.

| Area and source | Finding and implication |
| --- | --- |
| Running steering: [chat_routes.py](../../src/operator_api/chat_routes.py), [server/chat.py](../../src/server/chat.py), [middleware/steering.py](../../src/graph/middleware/steering.py) | POST `/api/chat/sessions/{session_id}/steer` (line 2044) calls `steer_or_start_turn` (chat.py line 1009). A running or parked session queues input. Middleware drains it into a `HumanMessage` (steering.py line 119) before the next model call and emits `steer_consumed` with the queued text (lines 56, 71). This continues the original session, but does not make that text private. |
| Peer delivery: [executor.py](../../src/a2a_impl/executor.py) | The `steer_consumed` handler (line 1479) publishes its payload through the task updater and mirrors it to the progress hook. `_flush_text` (line 1182) and terminal artifact emission also publish the answer. A steer made while the turn is running folds into the peer's own task, whose output is the peer's reply. Removing text from the consumed event alone leaves answer and persisted-history exposure paths. |
| Operator-tier peers: [auth.py](../../src/a2a_impl/auth.py) | `_classify_token` (line 481) grants the **operator** tier at full scope to the fleet service token (ADR 0089) and to paired devices (ADR 0087); an agent device gets full scope (ADR 0121 D4). Most example participant roles (Claude Code for Josh, other fleet agents) therefore sit in the same `OPERATOR_OWNER` group as the console: they can `GetTask` the operator's steer turn, and an agent device can read `/api/chat/sessions/{id}` directly. Decision 1 requires a new identity distinction — the human's console versus an agent with operator rights — before audience filtering means anything; filtering output surfaces alone cannot separate them. |
| Idle and completion races: [server/chat.py](../../src/server/chat.py) | `_session_turn_idle` (line 964) checks activity and pending interrupts under a thread lock. `_start_steer_turn` (line 883) sends a loopback `SendMessage` with the same `contextId` and a new task using `self_invocation_headers`; duplicate message IDs join the started task. Failed starts requeue. This is not a copied chat, but it does not prove privacy or restart-safe audience selection. |
| Idle steer vs peer ownership — **fixed**: [stores.py](../../src/a2a_impl/stores.py) | Issue [#5379](https://github.com/protoLabsAI/protoAgent/issues/5379) (an idle steer locked the federation peer out of its own thread) is **fixed** by [PR #5380](https://github.com/protoLabsAI/protoAgent/pull/5380) (commit `f9eea3c2`, issue closed 2026-11-16). Current behavior: `context_foreign` (line 501) assigns a conversation to whoever **started** it — the owner of its first-created task (SQLite rowid order). An operator steer into a peer's idle thread keeps the thread the peer's: the peer's next message is accepted, owner-scoped reads still hide the operator's turns from the peer, and an operator-started context stays closed to federation callers. Non-SQLite dialects keep the stricter any-foreign-task rule. Covered by [test_a2a_hitl_parked_routing.py](../../tests/test_a2a_hitl_parked_routing.py) (not run here). Thread ownership is settled; the privacy of the operator's turns on other surfaces is not. |
| Checkpoint binding: [thread_ids.py](../../src/graph/thread_ids.py), [hitl_routing.py](../../src/a2a_impl/hitl_routing.py) | Default checkpoint identity is `a2a:<session_id>` (line 31), but plugins can resolve it from request metadata. Idle checks resolve with no metadata; the loopback includes selected turn settings, not arbitrary original metadata. Same context alone therefore does not prove same checkpoint under custom resolvers. Parked routing and ownership must be validated together. |
| Ownership partitions: [stores.py](../../src/a2a_impl/stores.py), [hitl_routing.py](../../src/a2a_impl/hitl_routing.py) | Task owners partition operator and federation callers; operators can also read federation tasks. `check_context_owner` (line 477) rejects foreign-context continuation for non-operators. This is task-level protection, not a per-message boundary between private operator responses and peer deliverables on an existing task — and per the auth finding above, "operator" currently includes agents. |
| Derived content: [session_gist.py](../../src/graph/session_gist.py) | The gist writer skips only `background:`, `job:`, `palette-` and `system:` session prefixes, and incognito turns (around line 85) — so A2A peer threads get gists. Merged ADR 0132 work reads full conversations: [#5376](https://github.com/protoLabsAI/protoAgent/pull/5376) adds a gist per chat, [#5383](https://github.com/protoLabsAI/protoAgent/pull/5383) shows gists in the digest, [#5387](https://github.com/protoLabsAI/protoAgent/pull/5387) keeps closed chats in the digest for 14 days, [#5390](https://github.com/protoLabsAI/protoAgent/pull/5390) makes harvest summaries and facts cover the whole chat. Private steering text could therefore reach a gist, digest entry, harvested summary or fact, which a later peer-visible turn can draw on. Gist, digest, harvest and facts are audience boundaries, not an afterthought. |
| Observability: [tracing.py](../../src/observability/tracing.py), [prompt_capture.py](../../src/graph/middleware/prompt_capture.py), [trace_export.py](../../src/observability/trace_export.py) | Traces record prompts and tool input/output; `io_allowed` (line 1018) suppresses content inside an incognito turn. Prompt capture stores prompts and skips incognito sessions (line 125). `telemetry.fleet_trace_export` can ship traces off the machine. Whether private steering follows the incognito rule on these surfaces is open (settled in Phase 0); until decided, they count as peer-reachable output surfaces. |
| Existing open helper: [openSourceChat.ts](../../apps/web/src/work/openSourceChat.ts), [sessionHydration.ts](../../apps/web/src/chat/sessionHydration.ts) | The helper hydrates durable turns for the same ID, restores dismissed sessions, pushes reopen when dismissed, selects Chat and returns false on failure. Reuse these seams. The helper accepts a session ID, not an owner-agent/context pair; it is not sufficient for remote routing. Hydration starts with `SESSION_TURN_LIMIT = 50` (line 589) and needs full-history paging and speaker/audience review. |
| Chat index and registration: [chat_routes.py](../../src/operator_api/chat_routes.py), [session_registry.py](../../src/server/session_registry.py) | Turn-based session discovery filters `chat-%`; the server-held registry also supports explicit registration and reopen. Opening a non-chat original locally is not a durable discoverability contract. Register explicitly opened originals and verify resync, fresh-device and restart behavior; do not expose every job/system session by removing the prefix filter globally. |
| Incompatible continuation: [chat-store.ts](../../apps/web/src/chat/chat-store.ts), [ConversationReader.tsx](../../apps/web/src/chat/ConversationReader.tsx) | `continueConversation` (line 1613) calls `createSession` and seeds a draft in a new chat. The reader offers Continue in a new chat (line 290). Neither implements the approved original-chat flow. |
| Current Needs you: [NeedsYouSection.tsx](../../apps/web/src/chat/NeedsYouSection.tsx), [tasks.tsx](../../apps/web/src/work/sections/tasks.tsx) | Needs you offers **Open conversation** (line 119); non-chat threads open the read-only reader (C6b). Waiting-task details carry the D8 Reply form (line 230) and the shared **Started in chat …** provenance (line 180). Per decision 5, v1 keeps the structured decision controls and missing-origin fallback. |
| Origin and work: [tools/session.py](../../src/tools/session.py), [tasks/store.py](../../src/tasks/store.py) | `origin_session_for` (line 34) follows durable inbox/scheduler/background causes with bounded recursion and empty-on-unknown behavior. Tasks persist `session_id` and links; links normalize reference/kind/state/reason and can close tasks when resolved. Origin is not a unique work ID or a list of role-labelled original child transcripts. One conversation can cause multiple tasks. Board cards now record the conversation that caused them (bd-edo5o, [projectBoard-plugin #777](https://github.com/protoLabsAI/projectBoard-plugin/pull/777)); that is provenance groundwork this design should reuse, not a work-identity store. |
| Delegation continuity: [conversations.py](../../plugins/delegates/conversations.py), [acp_adapter.py](../../plugins/delegates/acp_adapter.py) | `_CONTEXTS` and `_PENDING` are process-local bounded `OrderedDict`s (`_MAX_ENTRIES = 512`, lines 127–134). They cannot be the durable source of related-thread links or retention references. An ACP coder's transcript exists only as the adapter's `transcript_tail` (line 380). A caller's `origin_session` is not the delegated child's transcript identity. |
| Retention: [conversation_policy.py](../../src/a2a_impl/conversation_policy.py), [stores.py](../../src/a2a_impl/stores.py) | The shared outside-conversation window is seven days (`CONVERSATION_WINDOW_DAYS = 7`, line 9). `sweep_expired_tasks` works on individual task timestamps, preserves console chats until tombstoned, and protects paused/restart-spared tasks. Excluded job/background/system/palette contexts normally retain the 24-hour TTL. These rules do not yet implement decision 3's sweep invariant for linked originals. |
| Pruned checkpoints: [chat_rehydrate.py](../../src/server/chat_rehydrate.py) | Merged [#5365](https://github.com/protoLabsAI/protoAgent/pull/5365) supplies summary-plus-recent-turn recovery for an aged-out checkpoint. It does not prove every original turn is retained, private responses are isolated, or a metadata-dependent remote original is safely resumable. |

## Required backend contracts, proposed for implementation review

### Private input and private response audiences

Preserve one original conversation identity, with server-enforced audience-aware
reads and delivery. The contract is decision 2: **steering text and the turns it
triggers are never delivered on peer channels — stream, poll, push, history,
tool output. The agent may act on what it was told and deliver intended
results. Relaying anything else needs an explicit operator instruction.** This
wording is deliberately met-able: the agent is allowed to use what it was told;
what is forbidden is delivering the private exchange itself.

Decision 1 defines the protected party: the human's own console session. An
agent holding operator-tier credentials (fleet service token, paired agent
device) is a peer for this contract, even though today's auth ladder classifies
it as operator. Phase 0/1 therefore needs an identity concept that separates
the human operator from agents with operator rights; filtering output surfaces
alone cannot do it.

Enforce the boundary at every output surface: streaming frames, GetTask/poll
results, push callbacks, durable task history, transcript reads, progress
events, tool outputs, artifacts, exports — and the derived and observability
surfaces below. Client-side hiding and a prompt telling the model to be
discreet are insufficient. Model output can quote private input, so filtering
only input events is insufficient too. Persist audience decisions and separate
deliverable output from private conversational responses within the existing
storage architecture.

The DS recommendation to default unknown output audiences to private is a
**proposal requiring technical validation**, including how authorized peer work
results still finish normally. The Phase 0 prototype compares two enforcement
models — the agent deciding each output's audience, versus an explicit
"deliver to requester" action — and the explicit action is the easier one to
enforce and test. Prove behavior for running, idle, parked, resume, completion
races, retry, restart, and subscribers established before the steer. Do not use
a side thread to obtain privacy at the cost of losing the original identity.

Explicit relay such as "tell them the deadline moved" is permitted by decision 2
only as an explicit operator instruction. The permission flow and UI are still
open (settled in Phase 0). Recommendation: make the intended recipient and exact
relay scope clear, authorize that delivery, and record it without disclosing
unrelated private exchanges. Do not infer permission to forward the entire
steering turn.

### Derived content, memory and observability boundaries

Audience enforcement must also cover content derived from the conversation:

- **Gist, digest, harvest and facts.** Per the findings table, peer threads get
  gists, digests show them, closed chats stay in the digest for 14 days, and
  harvest summaries and facts cover the whole chat. Each of these is an
  audience boundary: a private steer must not surface in a gist, digest entry,
  harvested summary or extracted fact that a peer-visible turn can later use
  verbatim.
- **Tracing, prompt capture and fleet trace export.** Whether private steering
  follows the incognito rule (`io_allowed` suppressing content; prompt capture
  skipping; export carrying no private content) is open: **settled in Phase 0.**
  Until settled, treat these as peer-reachable surfaces in every isolation test.
- **Memory and search recall** that can reach another audience remain in scope,
  as before.

### Two audiences in one context

If a peer sends a follow-up while the operator's steer is waiting in the same
context, one model reply could answer both. The rule is open (settled in
Phase 0). Recommendation, labelled as a proposal: **one audience per turn** —
peer turns queue until the private turn finishes, and vice versa — so no single
model call mixes audiences. The validation scenarios below include this case.

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

**Invariant (decision 3): no automatic retention sweep deletes a linked
original while any linked work is active.** An explicit operator delete — × on
a chat tab — still deletes the chat, as today; Work then shows that original as
unavailable, keeping the row's role, state and provenance. After the last
linked work closes, normal closed-chat retention applies. A recent tail,
summary or active task row is not a substitute for the earlier conversation.
Retain the original transcript/archive chain and the checkpoint, ownership and
links needed for safe continuation; do not rely on browser caches or delegation
memory.

Recommended policy, still to decide (settled in Phase 0): treat every
nonterminal work state as active, including queued/Coming up, blocked,
waiting/Needs you, parked and unresolved acceptance/recovery. Hold references
conservatively when a store cannot establish closure. Reconcile durable
references before pruning on restart. A shared original stays retained until
all linked jobs close, even if its last message is older than seven days. A
task turn ending, request promotion, or one child closing must not release the
other references.

Linked work that stalls and is never closed would hold its original forever.
Who closes it — and how ADR 0129 D9's stall nudges and Needs you parking
interact with retention release — is open (settled in Phase 0). The sweep
invariant protects against automatic deletion; it must not quietly become
unbounded growth with no owner.

Apply the keep to the **entire original**, including older terminal task rows,
compaction archives and required checkpoint state. Coordinate task TTL and
checkpoint pruning (see [checkpoint_prune.py](../../src/graph/checkpoint_prune.py));
protecting only the latest task would still lose the opening request. Persist
terminal release and reconcile idempotently so restart cannot release early or
keep abandoned references forever. Release makes the history eligible for normal
policy, not an instruction to delete it immediately. The closed-chat clock after
release must be reconciled with the ADR 0132 S6 14-day closed-chat digest
window ([#5387](https://github.com/protoLabsAI/protoAgent/pull/5387)); that
reconciliation is open (settled in Phase 0).

Do not promise deletion and silently retain data. A remote owner unable to
guarantee the keep needs a capability limitation and design revision before
that state is advertised as supported — in v1, remote rows are unavailable
(decision 4).

## Overlap check

Refreshed 2026-11-16 against current `main` (v0.246.1).

Merged since the first inspection, all relevant and folded in above:
[#5365](https://github.com/protoLabsAI/protoAgent/pull/5365) (checkpoint
recovery), [#5363](https://github.com/protoLabsAI/protoAgent/pull/5363) (digest
filtering), [#5366](https://github.com/protoLabsAI/protoAgent/pull/5366) (model
chains), [#5376](https://github.com/protoLabsAI/protoAgent/pull/5376),
[#5383](https://github.com/protoLabsAI/protoAgent/pull/5383),
[#5387](https://github.com/protoLabsAI/protoAgent/pull/5387) and
[#5390](https://github.com/protoLabsAI/protoAgent/pull/5390) (ADR 0132 S4–S7:
gists, digest gists, 14-day closed chats, whole-chat harvest — all read full
conversations and are named as audience boundaries above), and
[#5380](https://github.com/protoLabsAI/protoAgent/pull/5380) (the #5379
ownership fix).

Open pull requests at this inspection are dependency updates only; none touches
conversations, Work or From others. The live board was checked by the
2026-11-16 planning audit: **no open card overlaps this proposal**; the open
cards were ADR 0133 model-chain fixes. bd-ia8cj shipped as ADR 0132 S6
(#5387). bd-edo5o
([projectBoard-plugin #777](https://github.com/protoLabsAI/projectBoard-plugin/pull/777),
merged) makes board cards record the conversation that caused them and is cited
in the identity section as provenance groundwork. Recheck the board and open
PRs again when implementation is assigned; no cards are created by this
proposal.

## Proposed small phases

Per decision 6, work starts on a separate go from Josh, beginning with Phase 0.
These are review-sized phases, not implementation cards or dispatch
instructions. Full interactive navigation (a composer on an opened original)
still depends on audience isolation; the optional read-only milestone below is
the only navigation that may precede it.

| Proposed phase | Small deliverable and dependency | Exit evidence |
| --- | --- | --- |
| 0. Contracts and isolation prototype | Settle every "Open: settled in Phase 0" row in the decision map. Run a **time-boxed isolation prototype** on the real A2A seams comparing two enforcement models: the agent deciding each output's audience, versus an explicit deliver-to-requester action. GetTask returns whole task history and the peer protocol has no private-message notion, so isolation may prove infeasible as designed. Refresh board/PR overlap. | The prototype ends in an explicit **continue-or-revise** decision. Named unsupported states; no implementation inferred from visual sign-off. |
| 1. Audience isolation | Introduce the human-operator identity (decision 1) and enforce the decision 2 contract in existing transport/store seams, including derived content (gist/digest/harvest/facts) and observability per the Phase 0 ruling. Depends on Phase 0 deciding "continue"; precedes any steering from Work-opened originals. Named scenario: after a private steer, the peer still continues its own thread (ownership already fixed by #5380; privacy of the operator's turns remains to prove). | Adversarial stream/poll/push/history/tool-output tests with preexisting peer handles, including peers holding operator-tier credentials; authorized result delivery still works. |
| 2. Durable work relationships | Add request correlation, role-labelled original links and idempotent promotion to existing stores. Can be designed with 1; no interactive UI enabled. | Retry/restart/multiple-job fixtures prove identity, provenance and counts; owner authorization rejects forged links. |
| 3. Retention and continuation | Implement the decision 3 sweep invariant for all linked originals; reconcile references and closure; validate checkpoint metadata binding and #5365 interaction; apply the Phase 0 stall-release rule. Depends on 2. | Full-history and shared-link tests across TTL boundaries and restarts; missing checkpoint fails safely; × delete shows unavailable in Work. |
| 4. Original-chat navigation | Extend openSourceChat/hydration/registry with owner routing, durable reopen, capability checks and return-to-Work state. Depends on 2–3; the composer additionally depends on 1. | Fresh-client, reconnect, owner switch, missing data and parked-resume scenarios pass without a new chat. |
| Optional milestone: read-only Open in chat | Open the original in main Chat with the composer disabled. Depends **only on phases 2–4**: isolation matters only once the operator can send. Accept or reject this milestone in Phase 0. Removing From others still waits for a routine-thread home. | Read-only open preserves identity, history and return-to-Work; no send path exists. |
| 5. Work presentation and migration | Add conversation rows (capability table), request promotion presentation, replace Open conversation / Continue in a new chat entry points per decision 5, and update the stale docs listed under migration. Depends on 4 and approved routine/completed discovery. | Desktop/phone/accessibility review plus focused integration tests; old links still resolve safely. |
| 6. Remote and non-A2A participants | Upgrade unavailable capability rows (remote A2A, ACP coder, board coder, plugin reviewer, Discord) only as their transcripts become durable and owner guarantees are verified. | Per-type capability evidence; unsupported rows stay truthfully unavailable. |

**Phase 0 outcome (2026-11-16): REVISE** — see [Phase 0 outcome](#phase-0-outcome).
Phase 0 ended in revise, not continue, so **Phase 1 does not begin as written**.
Before Phase 1 (audience isolation), the design must be revised to: adopt
enforcement **Model B** (explicit deliver-to-requester) as the direction and make
it an authenticated delivery capability with recipient binding and scoped
operator-relay authorization, not a bare event
([#5400](https://github.com/protoLabsAI/protoAgent/issues/5400)); handle idle and
parked/resume private inputs that enter SDK task history before any output gate,
rather than filtering output surfaces alone
([#5401](https://github.com/protoLabsAI/protoAgent/issues/5401)); turn the
decision 1 human-console identity into a real transport capability (issuance,
rotation, revocation, proxy propagation), not a standalone classifier
([#5402](https://github.com/protoLabsAI/protoAgent/issues/5402)); and extend
audience exclusion to artifacts, tool output, files and the checkpoint-derived
and observability surfaces (gist, digest, harvest, facts, tracing, prompt
capture, fleet export) across later turns
([#5403](https://github.com/protoLabsAI/protoAgent/issues/5403)). Every phase
after Phase 0 still needs a further, separate go from Josh (decision 6); this
proposal adds no implementation cards or dispatch instructions.

Split each phase at its API/store/UI seam after approval rather than bundling
backend privacy and a visual replacement into one release.

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

These recently landed docs describe the superseded interaction and go stale when
implementation ships; revise them in the same change:
the From others guide ([#5353](https://github.com/protoLabsAI/protoAgent/pull/5353)),
the operator API reference rows for `/api/conversations` and the task/inbox
fields ([#5348](https://github.com/protoLabsAI/protoAgent/pull/5348)),
the retention pages ([#5349](https://github.com/protoLabsAI/protoAgent/pull/5349),
[#5351](https://github.com/protoLabsAI/protoAgent/pull/5351)) and the console
guide's Work section ([#5350](https://github.com/protoLabsAI/protoAgent/pull/5350)).

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

## Decision map: settled and open

Every product decision this proposal touches, disposed one way or the other.
"Decided" rows cite the numbered decisions of 2026-11-16; nothing else is
approved. Recommendations on open rows are labelled proposals, not approvals.
Rows informed by the [design revision](#design-revision-after-phase-0) read
**Proposed in design revision (pending Josh)**. Their issue references distinguish
executed Phase 0 evidence from inspection. Unrelated rows, including ADR 0131
amendment versus new ADR, stay open; no recommendation becomes a Josh decision.
See [Phase 0 outcome](#phase-0-outcome) for the historical result.

| Decision | Status | Recommendation where open (proposal, not approval) |
| --- | --- | --- |
| Who "the operator" is for private steering | **Decided by Josh 2026-11-16 (1)** | — |
| Privacy contract: what is never delivered, what the agent may do | **Decided by Josh 2026-11-16 (2)** | — |
| × tab delete vs retention; sweep invariant wording | **Decided by Josh 2026-11-16 (3)** | — |
| v1 participant scope; unavailable rows; no copied fallback | **Decided by Josh 2026-11-16 (4)** | — |
| ADR 0129 D8 structured controls and missing-origin fallback in v1 | **Decided by Josh 2026-11-16 (5)** | — |
| What merging this proposal means; when work starts | **Decided by Josh 2026-11-16 (6)** | — |
| Explicit relay ("tell them the deadline moved") | Resolved by Q1 — **Decided by Josh, 2026-11-16** — auto delivery; [#5400](https://github.com/protoLabsAI/protoAgent/issues/5400) | Section 1: a relay is an ordinary delivered result through the server-side deliver-to-requester action bound to a delivery record — no console grant, exact-payload release or preview. Immutable requester binding and the recipient projection still hold. |
| Human-console identity mechanism (decision 1 unchanged; Clarified by Josh, 2026-11-16) | Mechanism proposed in design revision (pending Josh) — [#5402](https://github.com/protoLabsAI/protoAgent/issues/5402); device-kind question **Decided by Josh, 2026-11-16 (Q3)**; deployment boundary **Decided by Josh, 2026-11-16 (Q2)** | Section 3: the human operator is the shared operator bearer plus paired `device`-kind devices (phones/browsers); the fleet service token, `agent`-kind devices and federation stay peers. The unforgeable device kind and credential type carry the distinction, propagated separately from the operator tier — no new console credential. Scoped proxy/loopback delegation; possession does not prove human presence. Limit in Questions for Josh: kind records how a device was paired, not who uses it. Q3 accepts this limit for v1 — anything paired as kind `device` counts as the human operator; no stronger per-human proof is required. Q2: private steering stays enabled everywhere, including where credential and tool isolation cannot be established; the risk that an agent obtains console authority is accepted; there is no per-deployment disable switch. |
| Private idle/parked transcript storage | Proposed in design revision (pending Josh) — [#5401](https://github.com/protoLabsAI/protoAgent/issues/5401) | Section 2: private partitions on existing records under one original identity; no private SDK history/shared checkpoint messages. |
| Gist, digest, harvest and facts across later turns | Proposed in design revision (pending Josh) — [#5403](https://github.com/protoLabsAI/protoAgent/issues/5403), inspection-only | Section 5: public projections, source-versioned derived records, stale-record quarantine and no private fact extraction. |
| Files, structured output, tools and terminal exits | Proposed in design revision (pending Josh) — [#5400](https://github.com/protoLabsAI/protoAgent/issues/5400); result-release model **Decided by Josh, 2026-11-16 (Q1)** — auto delivery | Section 6: the delivery record seals files and structured output at finalization; per decision 2 effectful tools need no console-authorized manifest, but tool arguments, output, progress, errors and files stay private unless part of the delivered result; unknown/unsandboxable effectful tools stay unavailable; private errors/cancellation cannot flush to peers. |
| Nonterminal active states for retention | Open — not informed by Phase 0 prototype | Include queued/Coming up, blocked, parked, waiting and unknown recovery; release only on durable closure of all references. |
| Stalled, never-closed linked work: closure owner; D9 nudges and Needs you parking vs release | Open — not informed by Phase 0 prototype | Give release an owner; a stall nudge is a prompt to close or continue, never an automatic release. |
| One request producing several jobs | Open — not informed by Phase 0 prototype | Preserve a request lineage and distinct child work IDs; decide whether the parent counts before implementing promotion/counts. |
| What makes an incoming thread an actionable request rather than routine conversation | Open — not informed by Phase 0 prototype | Require durable work intent/evidence; do not classify every A2A thread as Work or guess from a title. |
| Remote original capability beyond v1 (read, retention, steering) | Open — not informed by Phase 0 prototype | v1 shows remote rows unavailable (decision 4). Advertise verified owner capabilities separately; read-only where authorized; no composer until privacy and continuation are proven. |
| Routine-thread discoverability after From others is removed | Open — not informed by Phase 0 prototype | Evaluate the preview's Started by others group in Chat; approve its eligibility and visibility before removing the old entry point. |
| Completed work/history placement | Open — not informed by Phase 0 prototype | Keep finished work out of active counts as already designed; choose the history destination and stale return-link behavior explicitly. |
| Closed-chat clock after last closure vs the 14-day closed-chat digest window (#5387) | Open — not informed by Phase 0 prototype | Define one truthful clock; never promise deletion while retaining data. |
| Do traces, captured prompts and fleet trace export follow the incognito rule for private steering? | Proposed in design revision (pending Josh) — [#5403](https://github.com/protoLabsAI/protoAgent/issues/5403), inspection-only | Section 5 proposes data-lifetime audience partitions and source-versioned projections, with no content tracing/capture for private reads and public-only fleet export. Each derived surface still needs executed later-turn evidence. |
| Two audiences in one context (steering scheduling) | Mechanism proposed in design revision (pending Josh) — [#5400](https://github.com/protoLabsAI/protoAgent/issues/5400), [#5401](https://github.com/protoLabsAI/protoAgent/issues/5401); steering-latency scheduling **Decided by Josh, 2026-11-16 (Q4)** | Section 4 proposes immutable execution-segment audiences, durable ordered intake, safe-boundary yielding and matching guarded resume. Restart and completion-race proof are required. Q4: private steering waits for a safe boundary and peer work queues behind it; no implicit interrupt bypass or change to decision 5. |
| Default-private output enforceability; agent-decides-audience vs explicit deliver-to-requester (result release) | **Decided by Josh, 2026-11-16 (Q1)** — auto delivery; [#5400](https://github.com/protoLabsAI/protoAgent/issues/5400), [#5401](https://github.com/protoLabsAI/protoAgent/issues/5401) | Sections 1–2 and 6: authenticated Model B (deliver-to-requester) with **auto delivery** — no console grant, exact-payload release or preview; private storage outside SDK/shared checkpoint history; the delivery record seals the final envelope and files. Auto delivery contains the private exchange itself, not the model's wording, so it does not stop the model repeating what it was told inside a result. A per-steer "hold for my review" option is a possible later addition, not v1. |
| Read-only Open in chat milestone | Open — not informed by Phase 0 prototype | Accept it: it ships value from phases 2–4 without waiting on isolation. |
| Record the supersession as an ADR 0131 amendment or a new ADR (ADR home) | **Decided by Josh, 2026-11-16 (Q5)** | Record it as an ADR 0131 amendment so the contract has one home ("as recommended"). The amendment is written with Phase 1, after Josh's go; this card writes no amendment and edits no ADR file. |

## Focused validation and risks

Existing test files inspected or located as validation seams, **not run**:

| Existing test | Relevant coverage to preserve/extend; not proof of the new contract |
| --- | --- |
| [test_steer_idle_starts_turn.py](../../tests/test_steer_idle_starts_turn.py) | Idle start, running/parked queues, duplicate IDs, completion races, failure/requeue and loopback context. Add audience persistence and process-restart cases. |
| [test_steering.py](../../tests/test_steering.py) | Queue/drain and middleware injection. Add private responses and safe intended-result delivery, not just event redaction. |
| [test_a2a_hitl_parked_routing.py](../../tests/test_a2a_hitl_parked_routing.py) | The #5380 ownership rule: a steered peer thread stays the peer's; owner-scoped reads hide operator turns. Extend with operator-tier agent credentials (decision 1) and non-SQLite dialect behavior. |
| [test_chat_routes.py](../../tests/test_chat_routes.py) | Operator routes, session history and listing seams. Add owner authorization, explicit original registration and private history reads. |
| [test_work_origin.py](../../tests/test_work_origin.py) | Durable cause resolution, bounds/cycles and missing origins. Add related child transcript provenance and idempotent request promotion. |
| [test_a2a_stores.py](../../tests/test_a2a_stores.py) | TTL, paused/restart preservation and durable stores. Add all-linked-active references, terminal release, seven-day edges and partial transcript loss. |
| [openSourceChat.test.ts](../../apps/web/src/work/openSourceChat.test.ts) | Reopen/store wiring and leaving the Conversations stage, with mocked hydration. Add owner routing, failed reads, durable reopen and return state; mocks alone do not prove native backend continuity. |
| [test_checkpoint_prune.py](../../tests/test_checkpoint_prune.py) | Checkpoint pruning/recovery seam from #5365. Add linked-history retention and unsupported recovery handling without treating a summary as the original transcript. |

Required missing scenarios before enabling interaction:

1. A peer holds stream, poll and push handles before private steering — including
   a peer presenting operator-tier credentials (fleet service token, agent
   device; decision 1). Neither the steering text nor private response appears
   on any peer surface, including artifacts, history or tool output; the
   intended result still reaches the peer.
2. Operator history reads succeed; unauthorized peer reads and forged owner/context
   links fail. Equal context IDs on different agents never share caches or transport.
3. Exercise running, idle, parked, resume, completion race and process restart with
   a metadata-dependent thread resolver. Verify the same checkpoint identity,
   message deduplication and audience after each transition. After an idle steer,
   the peer continues its own thread (the #5380 rule) and sees none of the
   operator's turns.
4. Missing checkpoint, unavailable remote owner, partial history and a failed read
   produce distinct honest states. No path creates a chat or sends a replacement
   prompt. Recovery through #5365 does not hide missing earlier turns.
5. Open the same original twice, reload, reopen on another device, miss change-feed
   events, resync and return to Work. Identity, drafts, speaker attribution and
   progress remain correct; a stale local tab is not counted as persistence proof.
6. Retry promotion across a crash and stale refresh; keep one identity and count.
   Test two jobs from one request separately once that count policy is approved.
   Coder/reviewer links must reach their own original transcripts (or stay
   truthfully unavailable, per the capability table).
7. Keep an old original with two linked jobs across the seven-day boundary and
   multiple restarts. Close one job, then the last; verify complete history remains
   until release, and ordinary retention only becomes eligible afterward. Include
   queued/blocked/parked states once decided, archived spans and old terminal turns.
   An × delete during active work deletes as today and renders the Work row
   unavailable (decision 3).
8. Two audiences in one context: a peer follow-up arrives while an operator steer
   is queued (and the reverse). No single model reply mixes audiences; the chosen
   Phase 0 rule (for example, one audience per turn with peer turns queued) holds
   across park/resume and restart.
9. Derived content and observability: after a private steer, gists, digest
   entries, harvested summaries, extracted facts, traces, captured prompts and
   fleet trace exports carry no private steering content to a peer-reachable
   surface, per the Phase 0 ruling on the incognito rule.
10. Verify desktop rail/expanded and phone navigation, single action per row,
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

The largest risk is a convincing private UI over a shared output channel — made
sharper by the fact that today's auth ladder puts agents in the operator tier.
Next are private content resurfacing through derived artifacts (gist, digest,
harvest, facts, traces), false continuity from a bounded transcript or wrong
checkpoint, premature retention release, unbounded retention from stalled work,
duplicate work after promotion, and losing routine threads when removing their
only entry point. The phase gates address those risks before the visual
replacement ships. Retention also grows storage; monitor size without silently
weakening the sweep invariant. Missing isolation evidence is a gate on
readiness, not proof of a new defect in every consumer.

## Team-review checklist

- [ ] Confirm the approved original-Chat flow and single Open in chat action,
  including Needs you; no copied-chat fallback or Work conversation composer.
- [ ] Confirm the participant capability table and decision 4's v1 scope:
  requester row plus originals stored on this host; read-only and unavailable
  types named.
- [ ] Confirm Asked by/requester provenance, role-labelled related originals and
  promotion without duplicate identity/counts; decide multi-job semantics.
- [ ] Resolve routine-thread eligibility/discovery and completed-history placement.
- [ ] Confirm the decision 1/2 privacy contract and validate it on every output
  surface, with gist, digest, harvest, facts, tracing, prompt capture and fleet
  trace export covered as audience boundaries.
- [ ] Confirm the Phase 0 isolation prototype has a time limit, compares
  agent-decided audiences against an explicit deliver-to-requester action, and
  ends in continue-or-revise.
- [ ] Agree the rule for two audiences in one context.
- [ ] Accept or reject the read-only Open in chat milestone.
- [ ] Agree active-state, last-closure and stalled-work retention semantics,
  consistent with decision 3's sweep invariant and today's × delete.
- [ ] Confirm decision 5's D8 boundary: structured controls and missing-origin
  fallback stay in v1; any removal is a separate later decision.
- [ ] Accept additive migration/rollback on existing stores, durable original
  registration, owner authorization, server-authoritative resync, and the
  stale-docs list.
- [ ] Re-check the live board and open PRs when implementation is assigned; the
  2026-11-16 check found no overlapping open work.
- [ ] Review focused backend evidence plus desktop/phone/accessibility acceptance;
  do not confuse inspection, mocked tests or visual approval with runtime proof.
  Per decision 6, merging accepts direction and audience boundaries only;
  building starts on a separate go from Josh, with Phase 0 first.
