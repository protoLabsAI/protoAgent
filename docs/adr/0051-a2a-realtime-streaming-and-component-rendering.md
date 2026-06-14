# 0051 — A2A realtime streaming & component rendering

- Status: Accepted
- Date: 2026-06-14
- Builds on: ADR 0050 (background subagents), ADR 0039 (plugin event bus), ADR 0038
  (generative-UI artifacts), ADR 0006/0003 (the A2A terminal hook), and the
  `protolabs_a2a` DataPart/extension contract.

## Context

ADR 0050 shipped background subagents whose *completion* is surfaced (next-turn drain,
live in-chat push, a jobs widget) but whose *in-flight progress* is not — a background
job runs as a detached A2A turn and its tool-by-tool frames go to that turn's own A2A event
queue, which nobody reads (the manager fire-and-forgets the POST). The deferred "live
progress card" (bd-1sr) and the missing `stop`/`output` controls (Phase 4, bd-20c) both
stem from the same root: **we don't expose the realtime stream of a detached turn, and we
don't surface the task handle needed to control it.**

A protocol-alignment audit of the `a2a-sdk` (v2 / 1.0 proto surface) corrected two beliefs
baked into our docstrings and memory:

- **Cancel genuinely stops a running turn.** `CancelTask` → `ActiveTask.cancel` cancels the
  producer asyncio task, injecting `CancelledError` into `ProtoAgentExecutor.execute` (which
  unwinds the LangGraph stream). It is *not* a mark-only no-op. The only missing piece for a
  real `stop_task` is surfacing the A2A **task_id** (A2A is task-scoped — there is no
  cancel-by-contextId).
- **Live resubscribe exists.** `SubscribeToTask` re-attaches an SSE tap to a live in-process
  task. (Polling `GetTask` remains the *cross-restart* ceiling, since the in-memory task is
  gone after a restart.)

The audit also found the protocol mechanics solidly 1.0-conformant (correct method names,
error codes, version enforcement, SSE shape, durable + reconciled stores, SSRF-guarded push),
with small real gaps: no `TurnOutcome`/telemetry on cancel, no push-config TTL, and card
polish (`documentation_url`/`icon_url`/explicit top-level `protocol_version`).

Separately, the **component-rendering substrate already works end-to-end**: a typed DataPart
(`metadata.mimeType` discriminator + JSON payload) emitted over the A2A envelope, decoded by
the console's `dataByMime`/`*FromParts` helpers, and rendered by a React component — that's
exactly how `tool-call-v1` and the HITL `hitl-v1` card already work. Richer component
rendering is a new MIME + a render switch on that proven pipeline, not new plumbing.

## Decision

Expose the realtime stream of any turn through a small **executor progress/lifecycle hook**
(the same pattern as `set_terminal_hook`), and render richer components over the existing
**typed-DataPart** contract. Delivered in three slices.

### Slice 1 — realtime progress + background control (this ADR's first PR)

1. **Executor progress hook.** `a2a_impl/executor.py` gains `set_progress_hook(hook)` and
   fires `_notify_progress(context_id, task_id, frame)` at turn start (`turn_started`, which
   carries the task_id) and on each `tool_start`/`tool_end`. No-op when unset (like the
   terminal hook), so live turns — which already stream over their own SSE — pay nothing.
2. **`background.progress` channel.** A host hook (`server/a2a.py`) filters
   `context_id.startswith("background:")`, recovers the job_id (same path the terminal hook
   uses), records the A2A `task_id` on the job row on `turn_started`, and publishes
   `background.progress` on the event bus. The console's jobs widget threads these into a
   live per-tool card (reusing the chat `ToolCall` model), closing bd-1sr.
3. **`stop_task(job_id)`** — looks up the recorded A2A task_id and self-POSTs a real
   `CancelTask`; marks the job `canceled`. **`task_output(job_id, block, timeout)`** — reads
   the durable registry, optionally awaiting a terminal state (the cc-2.18 ergonomic).
4. **Foreground→auto-background.** A synchronous `task` delegation that exceeds a time budget
   transparently detaches to the background (returns a job id), so a long inline subagent run
   stops freezing the turn — the direct cure for the audited melt-down.
5. **Cancel-path telemetry.** `execute` records a `state="canceled"` `TurnOutcome` so a
   canceled turn isn't an observability hole and a canceled background job settles.

### Slice 2 — component rendering (generative-UI path A)

A typed **`application/vnd.protolabs.component-v1+json`** DataPart `{component, props}` (e.g.
`table`/`chart`/`timeline`/`status`), emitted over the A2A envelope like any other DataPart,
decoded by a new `componentFromParts`, and rendered inline in chat by a **curated registry**
of data-only widgets (no code execution → safe without a sandbox). Free-form generated UI
stays on the ADR 0038 sandboxed-iframe path (the `artifact` plugin).

### Slice 3 — alignment polish

Correct the cancel/resubscribe docstrings (+ memory); card polish; cheap realtime bus wins
(`scheduler.fired`, `goal.iteration`, `turn.usage`); audit that every outbound protoAgent
client sends `A2A-Version: 1.0` (a missing header defaults to `0.3` → `-32009`).

## Consequences

- **Detached work becomes observable and controllable** without changing the protocol — the
  progress hook is a host-side tap on frames the SDK already produces, and stop uses the
  SDK's real `CancelTask`.
- **One general seam, many uses.** The progress hook fires for every turn; today only the
  `background:` filter consumes it, but `turn.started`/`turn.progress` for all contexts is a
  trivial extension (Slice 3).
- **Component rendering rides the proven DataPart pipeline** — new widgets are a MIME + a
  registry entry, not new transport.
- **Cost note.** The progress hook is a no-op unless a host hook is registered; the
  `background.progress` publisher only fires for background contexts. Bus overflow drops
  oldest (progress is best-effort; `background.completed` is the source of truth).
- The auto-background time budget changes `task` behavior: a long sync delegation now detaches
  instead of blocking. Tunable; the model can still force foreground when it needs the result
  inline.

## References

- The A2A alignment audit + realtime/component research that informed this (the cancel/
  resubscribe correction; the executor-hook seam; the DataPart component contract).
- `a2a_impl/executor.py` (frame loop, `set_terminal_hook`), `server/a2a.py` (`_a2a_terminal`),
  `background/` (ADR 0050), `apps/web/src/lib/api.ts` (`dataByMime`/`*FromParts`).
