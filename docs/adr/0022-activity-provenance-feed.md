# ADR 0022 — Activity is a provenance feed, not a second chat

- **Status:** Accepted (2026-06-05) — refines the Activity surface of ADR 0003; implementation in 2 phases
- **Date:** 2026-06-05
- **Deciders:** Josh Mabry; protoAgent maintainers
- **Tags:** console, ux, reactive, activity, scheduler, inbox, provenance
- **Supersedes / Superseded by:** Refines the **Activity surface** of [ADR 0003](./0003-reactive-agent-activity-thread.md) (the reactive *machinery* stands; the *surface* changes).

> ADR 0003 built the reactive machinery — event bus, a durable Activity thread,
> an inbound inbox — and shipped it coherently. But the **surface** renders the
> thread as a second chat (`agent: <text>`) and **drops the provenance the
> backend carefully tracks**: every reactive turn is tagged with an `origin`
> (scheduler / inbox / webhook / a2a) and the inbox carries a `source` +
> `priority`, none of which reach the UI. For a surface whose whole job is *"the
> agent acted on its own — here's what and why,"* losing the *why* is the core
> failure. Activity becomes a **provenance-rich feed**.

---

## 1. Context & Problem statement

Auditing the shipped Activity surface (ADR 0003) found the machinery is solid —
event bus + `/api/events` SSE, durable thread (`system:activity` via the
checkpointer), inbox with `now`/`next`/`later` + StormGuard + dedup + the
`check_inbox` tool, the scheduler firing in. The **surface** is the problem:

- **Provenance is dropped.** The `activity.message` event publishes only
  `{role, text}`; `ActivityMessage` is `{role, content}`. The operator sees
  `agent: <text>` and cannot tell *why* the agent spoke — which schedule, which
  webhook, which inbox item. The `origin`/`source`/`priority` the fire paths set
  on the A2A message metadata never reach the UI, and `TurnOutcome` (the terminal
  hook's payload) doesn't carry `origin` either.
- **It's a second chat.** `ActivitySurface` duplicates the chat UI (log +
  composer) for one context. But reactive output is a *timeline of events* (a
  9:00 fire from job X, a 9:05 webhook Y), not a conversation.
- **The thread history can't carry provenance.** It comes from the checkpointer,
  which stores the conversation messages — not the trigger metadata. So a feed
  with provenance needs its own persistent source.

## 2. Decision

**Reframe Activity as a provenance feed**, backed by a small dedicated log;
keep the durable thread for continuation.

### 2.1 Thread `origin` through the turn

The executor reads `origin` + a human trigger label (job name/id, inbox
`source`, `priority`) from the incoming A2A message `metadata` and puts them on
`TurnOutcome`. The terminal hook then has the provenance without the executor
depending on the scheduler/inbox.

### 2.2 A small activity-event log

A dedicated SQLite `activity` table — `{id, ts, context_id, origin, trigger,
priority, text, task_id}` — written by the terminal hook for turns in the
Activity context. This is the **timeline/provenance** source; it's a different
concern from telemetry (cost/latency) and from the checkpointer (the continuable
conversation), so it gets its own home (small, like `inbox/store.py`). The
operator-set replies are logged too (`origin="operator"`).

### 2.3 The feed surface

- `GET /api/activity` returns feed entries **with provenance** (origin, trigger,
  priority, time, text) from the log — not raw checkpoint messages.
- The `activity.message` event carries `origin` + `trigger` so live entries are
  tagged.
- The console renders a **timeline**: each entry shows a trigger badge
  (⏰ scheduled · ↪ webhook · ✉ inbox · 🤝 sister-agent · 💬 you), time, priority,
  and the text. **Open** an entry → continues the underlying `system:activity`
  thread (the existing checkpointer conversation), so the feed is read-first,
  continue-on-demand.

### 2.4 What stays

The reactive *machinery* (ADR 0003) is unchanged — event bus, inbox tiers,
StormGuard, `check_inbox`, scheduler. Schedule and Inbox stay as Activity
sub-tabs. The single-thread model stays (per-source threads remain a future
refinement).

## 3. Consequences

- The reactive surface becomes **legible**: "why did the agent just do that?" is
  answerable at a glance — the headline fix.
- A small new store (`activity` table). One write per terminal Activity turn —
  negligible cost; the data was being discarded.
- The thread (checkpointer) is now the *continuation* target, not the primary
  view — a cleaner split (timeline = what happened; thread = the conversation).
- Surfaces the dormant value: once webhooks/sister-agents push (the fleet use
  case), the feed shows a real multi-source activity stream, not a flat chat.

## 4. Implementation (phased)

1. **Backend provenance** — `origin`/`trigger`/`priority` onto `TurnOutcome`
   (from message metadata); the `activity` log; enrich `activity.message` +
   `GET /api/activity`. Tests on the log + the threading.
2. **Feed UI** — render the timeline with trigger badges + open-to-continue;
   keep the composer for replying in the open thread.

## 5. Alternatives considered

- **Keep the second-chat, just add origin badges to messages.** Rejected: it
  leaves provenance unpersisted (history still bare) and keeps the wrong mental
  model (chat vs. event timeline).
- **Reuse the telemetry store for the feed.** Rejected: telemetry is cost/latency
  per turn and doesn't store the text; provenance is a distinct concern. A small
  dedicated log is clearer than overloading telemetry.
- **Per-source / per-job threads.** Still deferred (ADR 0003) — one thread +
  a provenance feed gives the legibility without the surface sprawl.
