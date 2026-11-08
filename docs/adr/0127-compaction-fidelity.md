# ADR 0127 — Compaction fidelity: the operator's words, the agent's commitments and its pending actions survive compaction

- Status: Accepted (2026-11-08; Josh: "yes plan the work and execute systematically")
- Date: 2026-11-08
- Deciders: Josh Mabry; protoAgent maintainers
- Implementation: S0–S5 done on `epic/compaction-fidelity` (epic #4848): S0 #4849 (ADR); S1+S2 #4851 (one engine, whole-span fold, continuation prompt); S3 #4853 (operator messages verbatim); S4 #4850 (scoped `working_state`); S5 #4854 (directive ledger).
- Completed follow-ups: #4856 (oversized human message regression test); #4866 (role-line hardening); #4870 (chunk budget and compaction-model window); #4873 (directive trust, bd-osrlp); #4877 (working-state cap and never-deferred directive tools, bd-9vwmw); #4872 (reserved `/directives`, bd-dz5f6). Console UI follow-up: #4861.
- Amends: [ADR 0101](./0101-context-lifecycle-log-surface-pressure.md) D4/D5 (the relief ladder and archive-first compaction) and [ADR 0079](./0079-autonomous-operating-model.md) (the `<working_state>` Observe block).

## Context

On 2026-11-07, protoEngineer and the operator agreed a release plan at 17:14:

> "once #4801 and #4808 part 1 have both merged, I'll cut the release … if the review panel gives the release PR a WARN or FAIL, I'll come back to you instead of merging."

At 18:28 the chat auto-compacted. The summary kept the operator's later "what are you waiting on? work is wrapped up" and nothing of the plan. When the operator next asked "what are you doing?", the agent decided it had started the release without approval. It apologised and said nothing would merge before the operator answered. Its own pending `wait` would still have squash-merged release PR #4847 and tagged v0.231.0 on PASS. Someone outside the chat cancelled that wait two minutes before it fired.

All three compaction paths had the same flaws:

| Path | What the summarizer sees | Prompt |
|---|---|---|
| Auto (`graph/middleware/compaction.py`, langchain `SummarizationMiddleware`) | The **last 4,000 tokens** of the span being replaced. `trim_tokens_to_summarize` was never set, so the default (`strategy="last"`) applies. | langchain's stock 4-section prompt, written in the user's voice ("The user considers…") |
| Manual `/compact` (`graph/compaction_op.py`) | The **last 16,000 characters** of a render that omits tool results | The long-term-memory digest prompt ("a few sentences… for searchable memory") |
| Overflow emergency (`server/chat.py` → `compact_thread(force=True)`) | Same as `/compact` | Same as `/compact` |

In the incident, the approval sat about 21K characters back in the replaced span, behind about 30K characters of tool output (one PR diff). The summarizer never saw it. The span also began with the 01:08 compaction's summary, so that summary was trimmed away too. Each compaction can silently drop the one before it.

None of the prompts has a place for **standing instructions or approvals**, the **agent's own commitments**, or **pending self-scheduled actions**. Outside the transcript, ADR 0079's `<working_state>` already re-injects pending schedules every turn. But it lists every job on the agent, unscoped and capped at 10, and cuts each resume instruction to 60 characters. The agent saw "…The operator approved it, and Jo" and never the "merge on PASS" that followed.

Outside evidence points the same way. Codex re-inserts real user messages verbatim after its summary (`codex-rs/core/src/compact.rs`, `COMPACT_USER_MESSAGE_MAX_TOKENS = 20_000`). Claude Code reportedly makes its compact prompt list *all* user messages and pending tasks with direct quotes, and its docs say instructions given only in conversation can be lost. Anthropic's compaction API warns that instructions in a summarized range stop applying unless restated. "Lost in Context Compaction" (2026) measured 17% retention of side constraints through compactors, and above 90% when constraints are kept in a separate store and re-injected.

## Decision

**D1 — One compaction engine for all three paths.** A host-free `graph/compaction_engine.py` builds the replacement context. The auto middleware, manual `/compact` and the overflow path all call it, and none of them uses langchain's summary step or the memory-digest prompt any more. The existing guarantees stay with their paths: archive-first (0101 D5), the keep-tail cutoff that never splits a tool call from its result, the auto path's failure backoff, the manual path's strict refusal, and the overflow path's shrink-or-stub safety valve.

**D2 — The summarizer reads the whole span.** There is no tail trim. The engine renders the span (human and AI text in full; each tool result clipped to head plus tail with an explicit omission marker, because operator intent never lives inside a tool's output). It splits the render into chunks that fit the summarizer's budget and folds them oldest to newest, carrying a running summary. When the span starts with a previous compaction's summary, that summary seeds the fold, so nothing earlier is regenerated from scratch or dropped. If the engine can't see the whole span, it fails. It never compacts from a partial view. On the auto path a failure skips compaction and backs off; on the manual path it refuses; on the overflow path it uses the honest stub, and D3 still applies.

**D3 — The operator's words are carried verbatim, never paraphrased.** The replacement context ends with an `<operator_messages>` block. It holds every operator message from the compacted span, oldest to newest, carried forward from any previous block, with timestamps where known. It's capped at about 20K tokens and keeps the newest first; anything older becomes a pointer to the archived history entry. Scheduler wakes and other system-generated prompts are labelled as such, not presented as operator messages. Because the block travels from compaction to compaction, an instruction survives any number of compactions until it falls out of the budget.

**D4 — A continuation prompt with required sections, written as the agent's own notes.** Each section is filled in or marked `None`:
- standing instructions and approvals, quoted verbatim with their conditions and stop rules, plus whether each is fulfilled, pending or revoked;
- commitments the agent made to the operator;
- pending self-scheduled actions (waits, watches, delegations) and what each will do;
- session intent, state and decisions, artifacts, next steps.

The summarizer is told never to infer an approval or a revocation, and to quote rather than paraphrase. The message is labelled as the agent's notes ("written by you when earlier conversation was compacted — not a message from the operator"), not as the user's voice.

**D5 — `<working_state>` shows the agent its own pending actions in full.** Pending schedules and watches are scoped to the current chat. Other chats get a count only. Each entry shows its full resume instruction (capped generously), its next fire time, and the label "runs automatically unless you cancel it". The agent can no longer tell the operator that nothing will happen while its own queued action says otherwise.

**D6 — A per-chat directive ledger.** Operator directives and approvals ("cut the release once A and B merge; stop on WARN/FAIL") live in a durable, chat-scoped store, independent of the transcript:
- The agent records one with `record_directive` (the verbatim quote, any conditions, and its scope), reads them with `list_directives`, and closes one with `revoke_directive` or `fulfil_directive`, giving a reason.
- The operator sees and revokes them through `GET`/`DELETE /api/chat/sessions/{id}/directives` and the `/directives` chat command.
- Every in-force directive is injected each turn under `STANDING DIRECTIVES` in `<working_state>`.
- The agent is told to record a directive when the operator approves or instructs something that outlives the current turn, and to consult the ledger before deciding it lacks approval.

The ledger is the system of record. The transcript is not.

**D7 — Fidelity is tested against the incident.** A synthetic fixture with the incident's shape runs through all three paths: a conditional approval, then more than 30K characters of tool output, then a casual remark, with a pending wait carrying an action. Each must keep the approval and its conditions verbatim and show the pending action. The fixture fails on the code this ADR replaces.

## Consequences

- A compaction costs one summarizer call per chunk instead of one. Compaction is rare (around 0.8 of the window), and the auto path already backs off on failure.
- The kept context carries up to about 20K tokens of verbatim operator messages. That is still well inside the 0.8 trigger on current windows.
- A fork's custom summary prompt, if any, stops applying. The continuation prompt is the contract.
- Console UI for the ledger is a follow-up that needs visual review. This ADR ships the API and the `/directives` command.

## Rejected alternatives

- **Only set `trim_tokens_to_summarize=None`.** The summarizer would then see the whole span, but it would still paraphrase the operator, still have no place for approvals, and still lose everything after a few compactions. On a huge span it could also overflow the summarizer's own window. It fixes one cause of four.
- **Never compact chat threads.** Compaction is the overflow safety valve. Without it, long chats end in a context error.
- **A second model call that checks each summary for contradictions.** There's no measured evidence it helps. The verbatim block (D3) and the ledger (D6) make the important facts independent of the summary's quality instead.
