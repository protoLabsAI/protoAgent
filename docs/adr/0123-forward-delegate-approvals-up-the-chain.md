# ADR 0123 — Forward delegate approvals up the delegation chain

- Status: Accepted (operator chose #4506 option (c), 2026-10-25)
- Date: 2026-10-25
- Implementation: Not implemented; this change records the design only.
- Issue: #4506
- Builds on: [ADR 0089](./0089-intra-instance-trust-boundary.md) (authenticated
  operator and fleet-service-token boundary).

## Context

A lead delegates over A2A to a sister protoAgent. The sister parks on a shell
command approval, but the lead receives a text tool result rather than an
approval card. The operator's answer can consequently become a model-written
sentence that does not satisfy the delegate's gate.

Grounding below was read from local `main` at
`2f49f8afc799341fbea3663ce34df5632beb369a`; line numbers refer to that revision:

| Source | Current behavior |
| --- | --- |
| `src/tools/fs_tools.py:1623–1634` | `interrupt({"kind": "approval", "title", "detail", "project"})` precedes `_approved(decision)`. Detail includes the actual `runs via:` executable chain. |
| `src/tools/fs_tools.py:419–429` | `_approved` accepts a bool, `{approved: bool}`, or a dict's `decision`/`answer` string. After strip and lower, strings must exactly equal `approve`, `approved`, `yes`, `y`, `true`, or `ok`. |
| `src/a2a_impl/executor.py:71,1351–1378` | The park is `input_required`, with `_hitl_prompt` text and the full payload in a DataPart using `application/vnd.protolabs.hitl-v1+json` (`HITL_MIME`). |
| `plugins/delegates/a2a.py:78–89,1182` | `_park_message` reduces the park to text and advises `delegate_to(query='<your answer>', resume_task_id=…)`; `ask_human` can bubble the question up as text. |
| `plugins/delegates/a2a.py:992–993,1089–1122` | Resume sets `taskId`, reads GetTask, restores its `contextId`, and then sends the answer. Working tasks are collected; terminal tasks return without sending. |
| `src/a2a_impl/executor.py:1128–1134`; `src/server/chat.py:1079` | The executor passes text and `resume` to the chat driver, which uses `resume_value=(message if resume else None)`. |
| `src/server/chat.py:641–690` | Pending interrupts are selected and resumed by interrupt ID; parallel gates cannot safely use an unqualified answer. |

### Exact-match reproduction

Verified on 2026-10-25 with this dependency-free reproduction of the actual
function on `main` (AST extraction avoids importing or executing shell tools).
Run from the repository root with `python3` (the available interpreter here):

```python
import ast
import subprocess

source = subprocess.check_output(
    ["git", "show", "2f49f8afc799341fbea3663ce34df5632beb369a:src/tools/fs_tools.py"],
    text=True,
)
function = next(
    node for node in ast.parse(source).body
    if isinstance(node, ast.FunctionDef) and node.name == "_approved"
)
namespace = {}
exec(compile(ast.Module(body=[function], type_ignores=[]), "fs_tools.py", "exec"), namespace)
for value, expected in [
    ("Approved — you may run gh/git…", False),
    ("not approved", False),
    (" approved ", True),
    ("yes", True),
    ({"approved": True}, True),
    ({"approved": False}, False),
]:
    assert namespace["_approved"](value) is expected
```

All six assertions passed. This confirms the proposed failure mechanism:
“Approved — you may run gh/git…” declines the command. It does not establish
that this exact sentence was the historical wire payload in #4506; no live
incident replay was performed. Loosening `_approved` is **not** the fix:
substring matching would also accept “not approved”.

## Decision drivers

- Show the real command, runner and project to the lead's operator.
- Carry the decision without model paraphrasing or implied consent.
- Never repeat the original delegation because a tool resumes from its start.
- Bind consent to one authoritative parked interrupt, including across retries,
  restarts, parallel gates and multiple delegation hops.
- Preserve existing question/form flows and unattended behavior; grant no new
  authority to unauthenticated or lower-trust callers.

## Considered options

1. **(a) Loosen approval text parsing.** Rejected: ambiguous sentences and negation
   are unsafe, and this still loses the approval card and its provenance.
2. **(b) Keep the text park and instruct the lead to relay an exact approval
   token.** Rejected: the model remains responsible for translating operator
   intent, and the operator does not see a real delegate approval card. The text
   park remains useful for questions and unattended status, not as authorization.
3. **(c) Forward the approval as a real card and return a structured decision.**
   Chosen by the operator on 2026-10-25. Trusted runtime code carries approval;
   the model may request forwarding but cannot supply the decision.

## Decision

### D1 — Raise the card only on the resume path, before SendMessage

The initial `delegate_to` SendMessage continues to return today's text park,
including its task handle. For a typed approval park, its guidance asks the lead
to call `delegate_to(resume_task_id=…)` to present the pending card; `query` is
not an approval. This extra tool call is a deliberate cost of the scheme.

On that resume path, after the side-effect-free GetTask and before **any**
SendMessage, recognize the current hitl-v1 approval and call LangGraph
`interrupt()` with its forwarded payload. LangGraph re-executes a suspended
tool from the top: on replay this path repeats only reads before the interrupt,
never the original query's SendMessage. No interrupt is added after an initial
send. Preserve working-task collection, missing-task errors and terminal-task
returns. A subsequent gate returned by the decision send is another text park;
it requires a fresh resume call rather than a second interrupt after that send.

Use the persisted local pending interrupt as the authority for the forwarding
binding: remote agent endpoint identity, context ID, task ID, interrupt ID and
payload digest. Capture that binding with the local card. Re-read GetTask on
replay, but compare its binding to the **saved card**, not to newly constructed
arguments alone. A changed interrupt/payload cannot inherit the old answer;
return stale-card status and require a new card. Never assume an interrupt's
position in a tool is its identity. The delegate validates the same binding
atomically when consuming the response (D4), covering changes after GetTask.

This chooses the resume-only scheme over a dedicated graph forwarding step or
a durable `tool_call_id` cache for the initial send. The former needs broader
graph plumbing; the latter must persist a remote result across the crash window
between send and local acknowledgement. We still need durable single-use
consumption at the receiving gate, not an initial-send cache.

Only an attended operator turn, or an authenticated delegation with an upstream
attended route, raises this forwarding interrupt. Attendance is derived from
trusted turn context, not model arguments or a caller's unverified claim.
Background delegations without such a route and autonomous turns retain today's
text park and no-operator handling. They never synthesize an affirmative reply.
Missing structured capability or malformed payload also falls back to text;
that fallback is informational and cannot approve a command.

### D2 — Preserve the request and show its provenance

Copy the delegate payload without rewriting `title`, `detail` (including every
`runs via:` line), `project`, or other request fields. Add a separate forwarding
envelope containing the binding and ordered hops (delegate name, endpoint
identity and task ID at each hop). Preserve the original payload inside that
envelope as well, so provenance cannot overwrite a delegate field.

The card visibly identifies “Approval requested by delegate”, the origin name
and task ID, and the full lead-to-origin chain. Use the existing bounded inert
`display` table for provenance, rendered by `HitlDisplay`, keeping the request
text separate. If an original display exists, retain it in the original payload
and compose a validated table without losing its content; if that cannot be
done within display limits, fall back to text rather than showing an incomplete
approval. Hop limits are 16; reject cycles or over-limit chains without granting
consent. Treat names as escaped display text, never credentials.

Forwarded cards set the local presentation's `session_allow=false`, suppressing
“Approve & don't ask again”. This does not mutate the original payload. Their
only outcomes are approve once or decline; existing local approval controls
remain unchanged. No provenance renderer is needed beyond the existing table,
but console tests must prove that the provenance and runner are both visible.

### D3 — Structured downward wire, never a textual approval

Extend hitl-v1 with a distinguishable response DataPart, for example:

```json
{
  "kind": "approval_response",
  "task_id": "delegate-task",
  "context_id": "delegate-context",
  "interrupt_id": "delegate-interrupt",
  "payload_digest": "digest-of-the-original-request",
  "approved": true
}
```

The request DataPart advertises this response capability and its binding.
`approved` must be a JSON boolean, not a truthy value or string. The resume
message still carries A2A `taskId` and `contextId`; they must agree with the
response and parked record. Optional text is explanatory only. Reject multiple
or conflicting response parts rather than choosing one.

The console sends a typed decision through explicit HITL-response metadata,
separate from chat text. The lead validates its local card binding, resumes
that forwarding interrupt with a structured decision, and builds the downstream
DataPart in runtime code. Do not expose an `approved` parameter to the model's
`delegate_to` tool schema or parse JSON-looking query text as a response.

The delegate executor authenticates and validates the response, then supplies
an internal, validated resume object to the chat driver through its existing
stream-factory boundary. The driver targets the saved interrupt ID with
`resume_value={"approved": bool}`. Transport metadata is untrusted input until
validation; it must not be able to impersonate this internal object. Lower
layers must not import `server` to implement this handoff.

For remote A2A approval parks, text alone, including exactly `approve`, `yes`,
or JSON-looking `{"approved": true}`, never reaches `_approved` as consent.
It leaves the gate pending with a structured-response-required result. This
intentionally retires remote text-as-approval behavior; it does not loosen or
change `_approved` or the existing direct local approval path. `ask_human` and
forms keep their current text/JSON flows and are out of scope for forwarding.

### D4 — Trust, single use and fail-closed consumption

Only the transport-authenticated **operator-tier A2A principal**, including the
loopback fleet service token under ADR 0089, may submit a structured approval.
A valid federation credential, open-mode access, a name in metadata, a hop
list, or possession of task/interrupt IDs is insufficient. Apply existing task
access checks too. The fleet token never travels off-box; remote peers require
their own operator-authorized credential. Provenance is explanatory, not proof
of authority. A trusted fleet service can assert an operator decision: this
inherits ADR 0089's shared-service trust, not end-to-end device signatures.

The authoritative record is the durable parked task/interrupt, bound to its
context, origin and immutable payload digest, with pending/consumed/expired
state. Validate and claim the pending response under the same serialized
checkpoint/resume operation; persist the chosen decision before dispatching
execution. A repeated identical response returns the recorded disposition,
never runs the tool twice. A conflicting response, wrong binding, new interrupt
on the same task, expired gate or already completed task cannot consume consent.
Recovery continues the recorded checkpoint transition rather than issuing a new
command. This is single-use authorization, not a promise of exactly-once shell
side effects after arbitrary process failure.

A decline delivers `{"approved": false}` once and the command does not run.
Dismissal is a decline. A timeout/disconnection grants nothing: an unanswered
card remains pending until its existing lifecycle expires or cancels it; expiry
invalidates its binding and late responses fail. No new timeout auto-approves,
no bypass setting changes, and later commands require their own gates.

**Testable invariant:** for an A2A approval park, an unauthenticated or untrusted
caller and every free-text resume produce zero approved resumes and zero
command executions, even if the text is `approve`. Only an authorized, typed,
boolean decision matching the still-pending task/interrupt may consume that
gate; false, missing, stale, conflicting, timed-out or replayed consent cannot
run a command. Tests must cover the complete set together, not successive
patches around independently inferred state.

### D5 — Multiple hops use the same protocol

For x → y → z, z parks and y receives the text handle. On y's resume-only
forwarding call, y raises a local approval interrupt carrying z's unchanged
request and the y → z binding. If y is itself a delegate with an attended
upstream route, its executor publishes that interrupt as hitl-v1 input_required
to x. x's corresponding resume call presents a card with the x → y → z chain.
A middle agent needs no locally attached operator to relay this park.

The operator decides at x. x sends a structured response bound to **y's**
forwarding interrupt; y consumes that local decision and sends another response
bound to **z's** original interrupt. Persist the parent/child binding at each
hop. A response never skips a hop, borrows a sibling task's consent, or becomes
text for a model to translate. Every receiving hop checks its authenticated
principal independently. A missing trusted route or an older peer at any hop
stops structured forwarding and leaves an informational text park, without
approval. Cancellation, decline and expiry at any hop cannot authorize z.

## Consequences

- The lead's operator sees the actual requested action, runner, project and
  delegate chain; one click can satisfy precisely the originating gate.
- The extra resume tool call is required before a new delegate park becomes a
  card. Models can still fail to request that forwarding; the text handle makes
  the remaining park visible and recoverable without granting permission.
- Structured clients, durable bindings and replay-safe response consumption
  add implementation work. A mixed-version fleet degrades to a visible park,
  not an approval guessed from prose.
- Remote text-only approval clients must adopt the typed response. Local
  approval parsing, shell policy guards, auto-approve rules, questions, forms,
  task collection and unattended handling otherwise keep their behavior.
- This ADR ships no runtime changes. The slices below implement and test the
  contract before claiming #4506 is fixed.

## Ordered implementation slices

Each card lists at most four files **including its own changelog fragment**.
New test modules below are planned, not files added by this documentation PR.
Dependencies are linear, S1 → S2 → S3 → S4 → S5; deploy the complete chain only
after S5. Existing regression suites supplement the named new tests.

1. **S1 — Delegate wire validation and durable consumption.** Files:
   `src/a2a_impl/executor.py`, `src/a2a_impl/approval_responses.py` (new),
   `tests/test_a2a_approval_responses.py` (new), `changelog.d/<S1>.added.md`.
   Publish binding/capability, validate transport principal and response parts,
   persist consumption with the task checkpoint, and expose a validated internal
   handoff. Tests: `test_operator_boolean_response`,
   `test_untrusted_and_open_mode_cannot_approve`,
   `test_text_and_json_text_cannot_approve`,
   `test_stale_wrong_task_conflicting_and_expired_responses`,
   `test_concurrent_duplicate_and_restart_consumption`. Until S2 consumes the
   handoff, advertise no active response capability and execute nothing from it.
2. **S2 — Targeted graph resume.** Files: `src/server/chat.py`,
   `tests/test_forwarded_approval_resume.py` (new),
   `changelog.d/<S2>.added.md`. Consume only S1's validated handoff, target the
   saved interrupt, and enable the capability once this consumer is wired.
   Tests: `test_structured_approval_reaches_exact_interrupt`,
   `test_parallel_interrupt_does_not_borrow_consent`,
   `test_decline_and_expiry_never_run_command`,
   `test_local_approval_question_and_form_unchanged`.
3. **S3 — Lead resume-only forwarding and provenance.** Files:
   `plugins/delegates/a2a.py`, `tests/test_delegate_approval_forwarding.py` (new),
   `changelog.d/<S3>.added.md`. Raise only after GetTask; persist the card binding,
   preserve payload, build provenance display, disable session bypass and send
   the typed response. Tests: `test_interrupt_replay_never_resends_initial_query`,
   `test_changed_gate_after_card_requires_new_consent`,
   `test_payload_runner_and_project_preserved`,
   `test_background_autonomous_and_legacy_text_fallback`,
   `test_working_terminal_and_second_gate_paths`.
4. **S4 — Typed console decision transport.** Files:
   `apps/web/src/chat/useHitl.ts`, `apps/web/src/lib/api/chat.ts`,
   `apps/web/src/lib/api/chatHitlResume.test.ts`,
   `changelog.d/<S4>.added.md`. Send bound structured metadata for forwarded
   cards, while preserving existing local submissions. Tests:
   `forwarded approval sends typed boolean and binding`,
   `chat text cannot create approval metadata`,
   `decline preserves binding and sends false`.
   Run existing `HitlDisplay.test.tsx`, `useHitl.test.tsx` and
   `apps/web/e2e/hitl-display.spec.ts` coverage for the reused provenance table;
   verify visible origin, full hop chain, runner and absent bypass action in
   S5's integration fixture. No new provenance component is required.
5. **S5 — Multi-hop and live integration acceptance.** Files:
   `plugins/delegates/a2a.py`, `tests/integration/test_fleet_a2a.py`,
   `apps/web/e2e/delegate-approval-chain.spec.ts` (new),
   `changelog.d/<S5>.added.md`. Complete recursive parent/child binding and
   exercise real authenticated A2A x → y → z with a deterministic gated tool
   and an isolated harmless command counter. Tests:
   `test_three_hop_approval_executes_once`,
   `test_three_hop_decline_timeout_and_replay_execute_zero`,
   `test_untrusted_hop_and_cycles_fail_closed`; browser cases
   `delegate chain and runs via are visible`,
   `approve once has no bypass action`, and
   `operator decision reaches origin without text translation`.
   Run the full Python gate, opt-in live A2A suite, console unit suite and these
   E2E cases before treating the feature as implemented.
