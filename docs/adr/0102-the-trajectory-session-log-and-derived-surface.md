# 0102 — The trajectory: append-only session log + derived surface

Status: **Proposed** (umbrella #2806; the log half of ADR 0101's deferred
log/surface split, giving #2786 its home)

## Context

ADR 0101 fixed how a session's context *grows and shrinks*; this record fixes
what it *remembers about itself*. The 2026-08-17 audit's sharpest forensic
finding still stands after the Endless Context work shipped: **"what did the
model see on turn N" is unanswerable.** Prompt snapshots
(`observability/prompt_snapshots.py`) record the system prompt per call but
never the message list; the audit log keeps 200-char tool summaries; telemetry
keeps per-turn aggregates; and the checkpoint is a *current-state* snapshot —
pruned to the latest 2 rows and destructively rewritten by compaction (#2784
archives the text, but not the request shapes), pruning (#2782), rewind, and
fork (#2803). The `events/` bus is explicitly ephemeral.

The external validation is loud: DeepSeek Harness's most-praised capability
after cache discipline is its "trajectory" — an append-only event log under the
invariant **"model-visible means logged"**, with resume, fork, search, and
replay all defined as operations on that one stream. We shipped the *mechanisms*
of its context system in ADR 0101 without the architecture tax; this ADR ports
the remaining principle the same way — as a thin record spine over the stores we
already have, not an event-sourcing rewrite.

Two consumers motivate the shape:

1. **Forensics** — cost audits (the $58.83-turn investigation was done by hand
   from an exported transcript), incident reconstruction, and honest answers to
   "why did the agent do that."
2. **The surface** — derived-view compaction (#2786, deferred by ADR 0101):
   once the durable record exists, the model-visible view can safely become a
   projection instead of a destructive rewrite.

## Decision

**D1 — A per-session, append-only trajectory log of REFERENCES, not text.**
`instance_root/trajectory/<encoded-session>.jsonl`, one JSON line per event.
The per-model-call `request` event records the envelope as references: the
stable-prefix hash (already computed by prompt capture), the context-frame
message id, the ordered message refs (`{id, role, content_sha256, chars}`), the
bound-tools hash, and the model/params. `response` records usage and finish
state; `tool` records call id, name, args hash, result hash + true size;
`surface_op` records every history rewrite (prune/compact/rewind/fork/repair)
as `{op, removed_ids, inserted_ids, cause}`; `turn/start`/`turn/end` carry
origin/trigger/outcome. References keep the log cheap (no second copy of every
tool result) and safe to retain; the *bytes* behind a ref live where they
already live — checkpoint, chat archive, prompt snapshots.

**D2 — "Model-visible means logged" at the reference level, honestly bounded.**
Reconstruction of call N joins the log's refs against the checkpoint and the
`chat-archive:` namespace. When pruning or an unarchived force-compaction has
destroyed bytes, the log still *proves what was sent* (hash + size + position)
even though the text is gone — and the reader says so, rather than pretending.
An opt-in full-text mode (developer flag, `trajectory.full_text`) inlines
message text into the log for deep forensics and enables true
fork-from-any-point; it is off by default because it duplicates every byte the
model sees onto disk.

**D3 — The writer is a middleware + op hooks, not a new subsystem.** A
`TrajectoryMiddleware` at `wrap_model_call` (after PromptCache — it must see the
final request, the same ordering rule prompt capture follows) emits
`request`/`response`; the tool events ride the existing audit seam; the surface
ops are one-line emits added to the four existing rewrite sites. Best-effort
everywhere: a log failure never touches a turn. Rotation: size-capped per
session with whole-file retirement on the checkpoint TTL
(`checkpoint_max_age_days`) — the log outlives the checkpoint rows but not the
thread's own retirement.

**D4 — Read surface: reconstruction, search, replay.**
`GET /api/trajectory/{session}` (paged events),
`/api/trajectory/{session}/call/{n}` (the reconstructed request, with
per-message `available: true|archived|destroyed`), and a search endpoint over
event fields. The console gets a Trajectory view on the existing document-viewer
pattern — a turn timeline that expands to per-call envelopes. Replay in v1 is
*read* replay (step through what happened); live re-execution is out of scope.

**D5 — The derived surface (#2786) is Slice 5 of this ADR, unchanged in its
gate.** With surface ops logged and the full history durable, compaction and
pruning can move from destructive `RemoveMessage` rewrites to per-request
projections via `ModelRequest.override(messages=...)` — the checkpoint becomes
the log's live tail rather than the only truth. The ADR 0101 condition stands:
this slice starts only after the D6 pressure telemetry has accumulated enough
real-session data to size the checkpoint-growth trade (SqliteSaver stores full
state per row; keeping full history in the checkpointer needs the keep-2
pruning rethought against the log's existence).

**D6 — Relationship to existing stores: the trajectory is the spine, not a
replacement.** Prompt snapshots keep owning system-prompt text (the trajectory
refs their `stable_hash`); the audit JSONL keeps its operator-facing tool rows;
telemetry keeps aggregates. Nothing is migrated; the trajectory joins them.

## Slices

1. **S1 — writer**: `TrajectoryMiddleware` + surface-op emits + rotation
   (goldens: an agentic turn's log reconstructs byte-hash-identical envelopes).
2. **S2 — read API + console view** (D4, minus search).
3. **S3 — search** over event fields.
4. **S4 — full-text developer flag** (D2) + fork-from-any-point on top of it.
5. **S5 — derived-view surface** (D5 / #2786), telemetry-gated as decided.

## Rejected alternatives

- **Full event-sourcing (the DSH architecture)** — the log as the *only* truth
  with everything derived. Rebuilding the checkpointer/stores around a log is
  the complexity tax the DSH reviews criticize (~10x token/infra overhead
  reports); we take the invariant, not the architecture.
- **Full text by default** — doubles the disk write path for every turn and
  duplicates content three stores already hold; refs + hashes answer the
  forensic questions at a fraction of the cost, and the flag exists for the
  rest.
- **Extending prompt snapshots with message lists** — snapshots are a
  per-CALL system-prompt store with 30-day/5000-row trimming; the trajectory
  needs per-SESSION ordering, surface ops, and checkpoint-aligned retention.
  Bolting those on would make one store serve two contracts badly.

## Consequences

"What did the model see" becomes answerable for the log's lifetime; every
history rewrite becomes attributable; #2803's fork gains a path to
fork-from-any-point (full-text mode); and S5 turns ADR 0101's fold-never-
truncate into never-truncate-at-all. Costs: one JSONL append per model call and
per tool call (hashing is cheap relative to a model round-trip), one more
per-session artifact to retire, and — in full-text mode only — a second copy of
model-visible bytes on disk.
