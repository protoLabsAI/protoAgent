# 0107 — Unified, policy-bounded self-improvement reviews

- **Status:** Proposed
- **Date:** 2026-08-27
- **Related:** ADR 0054 (dream/distill), ADR 0081 (self-authored persona),
  ADR 0079 (autonomous operating model), #3069

## Context

protoAgent already has three durable self-curation mechanisms, but they are
siloed: manual/scheduled distillation, additive skill capture, and guarded
`SOUL.md` editing. The operator cannot express one coherent disposition such as
“review completed work, propose most changes, and automatically maintain only
private skills.” Running that review as an ordinary lead turn would also make
`propose` advisory rather than a security boundary because the lead retains its
full writer toolset.

## Decision

Add an off-by-default `self_improvement` policy with one master switch and
`off | propose | auto` modes for the post-work review, persona, and skills.

Post-work dispatch uses the existing scheduler for durable, same-session turns.
Runtime status warns when the policy is enabled but that prerequisite is unavailable.

1. An achieved goal or a closed task enqueues `/self-improve` in the same
   session. The lifecycle callback never performs an LLM call or artifact write
   inline, so goal/task completion remains non-blocking.
2. `self-improve` is a bounded built-in subagent. Its tool map is intersected
   with a mode-derived allowlist at dispatch: proposal runs receive reads plus
   `task_create`; automatic runs receive only the writers whose facet is also
   `auto`. Prompt text is not the policy boundary.
3. Goal/task text and recalled activity are explicitly untrusted data. The
   reviewer may do nothing when evidence is weak.
4. Skill update/delete is limited to editable private artifacts. Flat shared
   stores never receive automatic skill writers; layered stores remain safe
   because writes route to their private backend.
5. Before update/delete, file-backed skills are archived without rewriting
   their contents. A sidecar records the mutation session/reason. Learned
   database-only skills use the most faithful representation the index retains.
6. The privileged reviewer is the only bounded subagent eligible to receive
   `edit_soul`, and only when master + distillation + persona are all `auto`.
   It uses the same injected reload callback and soul-history rollback path as
   the lead tool. This narrowly amends ADR 0081's lead-only rule.
7. Memory is evidence-only in this version. Automatic memory mutation remains
   owned by `/dream`; it is not silently folded into the new policy without a
   separate provenance/rollback contract.

## Consequences

The operator gets one settings surface and opportunistic review at meaningful
completion boundaries without paying for a model pass after every chat turn.
`propose` is mechanically unable to mutate durable artifacts. Automatic writes
carry provenance and a rollback path, at the cost of a new privileged built-in
subagent and additional policy-specific tests.
