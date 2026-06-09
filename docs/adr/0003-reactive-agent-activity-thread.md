# ADR 0003 — Reactive Agent: Activity Thread, Event Bus & Inbound Inbox

- **Status:** Accepted (2026-05-30) — execution underway, slice by slice
- **Date:** 2026-05-30
- **Deciders:** Josh Mabry; protoAgent maintainers
- **Tags:** architecture, scheduler, reactive, a2a, streaming, inbox, security
- **Supersedes / Superseded by:** The reactive **machinery** stands; the **Activity surface** is refined by [ADR 0022](./0022-activity-provenance-feed.md) (a provenance feed, not a second chat). The **event bus** here is extended into a decoupled plugin pub/sub by [ADR 0039](./0039-plugin-event-bus.md) (topics + wildcards + in-process subscriptions; the SSE frame becomes topic-in-payload).

> Accepted. protoAgent gains a **reactive surface** so the agent can respond to
> stimuli that aren't a live user chat turn — scheduled prompts, webhooks, and
> sister-agent pushes. Three pieces: (1) an in-process **event bus** with a
> server→client SSE channel (`/api/events`) so the server can speak unprompted;
> (2) a single durable **Activity thread** (a well-known A2A context) where all
> agent-initiated output lands and can be opened and continued like any chat;
> (3) an authenticated **inbound inbox** (`POST /api/inbox` + webhook /
> sister-agent intake) with now/next/later priority tiers and a `check_inbox`
> tool. Delivered in slices: event bus → Activity thread → inbox.

---

## 1. Context & Problem Statement

protoAgent can *schedule* prompts (the `LocalScheduler` polls a SQLite `jobs.db`
and fires due jobs), but a fired job's response **surfaces nowhere**:

- On fire, `LocalScheduler._fire` POSTs to `/a2a` with `message/send` and **no
  `contextId`** (`scheduler/local.py`).
- The A2A handler mints a **fresh random context** for that message
  (`context_id = params.get("contextId") or f"a2a-{uuid4()}"`,
  `a2a_handler.py:1342`), runs the agent in a background task, and stores the
  result **in memory only**, evicting it after a 1-hour TTL.
- The fire even carries `scheduler_job_id` / `scheduler_kind` in metadata — and
  the handler **discards it**.

Structurally there is no way for that output to reach a human: chat sessions
live only in the browser's `localStorage`, and **all streaming is
request-scoped** — the React console opens an SSE stream *in response to a user
turn* and closes it when the turn ends. The server has no channel to push an
**unsolicited** message. So scheduled prompts execute and their answers
evaporate, and there is no general way for the agent to react to *any* external
stimulus.

We want the agent to be **reactive**: time-based triggers, external webhooks,
and sister agents in a fleet should be able to poke it, and the result should
land somewhere durable and visible that the operator can read and continue.

### Reference: ORBIS

ORBIS (`protolabsai/orbis`, a voice agent) solved the reactive problem with
patterns we adapt to protoAgent's text/console idiom:

- an in-process **SSE event bus** (`voice/sse_bus.py`, `/api/events`) — any
  component publishes; the UI subscribes; the foundational "server speaks
  unprompted" primitive;
- a durable **inbox** with `now` / `next` / `later` tiers — the runtime supplies
  material, the *agent* decides when to surface non-urgent items;
- **dedup + storm circuit-breaker** on the fire path (drops duplicate / runaway
  fires) and **stash-and-replay** when no client is connected;
- stimuli enter from several channels (timer, push endpoints, sister-agent A2A).

We drop ORBIS's voice/VAD-specific delivery policies (they exist to avoid
talking over a speaking user); the text analog is simpler — append to a thread
and push an event.

## 2. Decision

Add a **single durable Activity thread**, a **server→client event bus**, and an
**authenticated inbound inbox**.

### 2.1 Event bus + `/api/events` (the push channel)

An in-process async pub/sub (`events/bus.py`): `publish(event, data)` fans out to
every subscriber's bounded queue (drop-oldest on overflow — a slow console never
blocks a producer). A new SSE route **`GET /api/events`** holds the connection
open and streams published events to the console. It is **read-only,
server→client** — no client input, so it adds no new authority surface.

Event kinds (v1): `activity.message` (a turn completed in the Activity thread),
`inbox.item` (a new inbox item arrived). The console keeps one `EventSource`
open for the app's lifetime and routes events to the relevant surface.

### 2.2 The Activity thread (where reactive output lands)

A single well-known context, `system:activity`, which maps to checkpointer
thread `a2a:system:activity` — so it is **durable for free** via the SQLite
checkpointer already in place (ADR-less prior work; bound at compile time). All
agent-initiated output appends here:

- **Routing.** Reactive producers set `contextId: "system:activity"` on their
  A2A message (and tag `metadata.origin` = `scheduler` / `inbox` / `webhook` /
  `a2a`). The scheduler stops relying on a minted context. A normal user chat is
  unaffected — it keeps its own context.
- **Surfacing.** When a turn in the Activity context reaches a terminal state,
  the handler publishes `activity.message` to the bus with the assistant text +
  origin. The console's **Activity** surface appends it live.
- **Reading history.** `GET /api/activity` returns the thread's message history
  from the checkpointer (read LangGraph state for `a2a:system:activity`). The
  surface loads this on open. This is the first **server-side**, durable,
  enumerable conversation (chats are otherwise browser-only).
- **Continuation.** The operator can reply in the Activity surface — a normal
  `message/stream` into `contextId: system:activity` — so the background thread
  is also a real, continuable chat. An **unread** marker (last-seen vs. latest)
  drives a rail badge.

A single thread (not per-job, not per-stimulus) is the v1 choice: it is the
"always-going meta chat" we want, the smallest durable surface, and it keeps all
proactive context in one place the agent can see across fires. Per-job or
per-source threads are a possible future refinement.

### 2.3 Inbound inbox (the general stimulus channel)

A durable SQLite `inbox` table (`id, created_at, priority, source, text,
dedup_key, delivered_at`). Inbound channels write to it:

- **`POST /api/inbox`** (authenticated) — external systems / cron / scripts.
- **Webhook + sister-agent A2A** intake — land as inbox items too.

Priority tiers govern delivery, ORBIS-style:

| Tier | Behavior |
|---|---|
| `now` | Immediately fires an Activity turn (the item becomes a stimulus prompt into `system:activity`), subject to dedup/storm guards. |
| `next` | Queued; the agent surfaces it when it next calls `check_inbox` (or at the start of an Activity turn). |
| `later` | Background; only returned on an explicit `check_inbox(priority_floor="later")`. |

A **`check_inbox(priority_floor)`** agent tool lets the lead agent pull pending
items and decide when to surface them — delivery decisions stay with the agent,
never forced mid-turn. Surfaced items are marked delivered (read-once).

**Dedup + storm guard** on the fire path: dedup by `dedup_key`
(e.g. `job:<id>`, `webhook:<event_id>`) within a window; a storm threshold
(N fires / window) suppresses runaway producers after emitting one
"rate-limited" notice. This is load-bearing once anything external can trigger
turns.

### 2.4 Architecture sketch

```
 stimuli                          intake → durable store        delivery
 ───────                          ────────────────────          ────────
 scheduler ─┐                                                ┌─ /api/events (SSE)
 POST /api/inbox ─┤→ inbox table ─(now)→ fire Activity turn ─┤   server→client push
 webhook ─┤        (next/later)→ check_inbox tool            │
 sister A2A ─┘                                               └─→ Activity thread
                                                                (a2a:system:activity,
                                                                 durable checkpointer)
                                  ↑ all routed to contextId=system:activity ↑
```

## 3. Security & Safety

Reacting to outside stimuli means **untrusted input can initiate agent turns
(and tool use)**. Constraints baked into the design:

- **Inbound is authenticated.** `POST /api/inbox` and webhook intake require the
  same bearer / API-key the scheduler fire already presents (from config). No
  anonymous trigger path.
- **Same authority as a normal turn.** An auto-fired Activity turn runs the
  ordinary graph — identical tool allowlist, no elevated permissions. A stimulus
  cannot do anything a user prompt couldn't.
- **The push channel is read-only.** `/api/events` is server→client only and
  carries no client input; it grants no new capability.
- **Backpressure & anti-storm.** Bounded subscriber queues (drop-oldest) and the
  dedup/storm guard prevent a misconfigured or hostile producer from
  flooding the agent or the console.
- **Prompt-injection awareness.** Inbox text is third-party content; it is
  delivered as data the agent reasons about, the same trust level as any tool
  result it already handles. Operators should treat the inbound token as a
  secret and scope who can post.

## 4. Consequences

**Positive**

- Scheduled prompts finally have somewhere to go; the agent becomes genuinely
  reactive (time, webhook, fleet).
- First durable, server-side, continuable conversation — a reusable seam for
  future server-side chat history.
- A general push channel (`/api/events`) usable later for live runtime/state
  events, not just activity.

**Negative / costs**

- New always-open SSE connection per console; new SQLite table; the A2A handler
  gains origin-aware routing.
- A single Activity thread can grow long — relies on the existing checkpointer
  pruning / harvest-to-knowledge path.
- More moving parts on the trigger path (auth, dedup, storm) that must be
  correct to avoid loops.

## 5. Alternatives Considered

- **Per-job / per-source threads** instead of one Activity thread — more
  organized history, more surfaces to manage. Deferred; one thread first.
- **Route into a chosen existing chat thread** — most "native", but requires
  moving chat persistence server-side first (chats are browser-only today). A
  larger lift; the Activity thread is the incremental step toward it.
- **Inbox-only feed (no chat thread)** — good for notifications, weak as a place
  to converse. We keep the inbox *and* the thread: inbox is the intake/queue, the
  Activity thread is where conversation happens.
- **WebSocket instead of SSE** — bidirectional, heavier; unnecessary since the
  channel is one-way and the codebase already speaks SSE (A2A streaming).
- **Webhook-only delivery (existing `push_config`)** — fires a webhook on task
  completion, but needs a pre-registered consumer and surfaces nothing in the
  console. Complementary, not a substitute.

## 6. Implementation Slices

1. **Event bus + `/api/events`** — `events/bus.py`, SSE route, console
   `EventSource` subscription primitive. Prove push end-to-end.
2. **Activity thread** — origin-aware routing to `system:activity`, publish
   `activity.message` on terminal, `GET /api/activity` history read, the
   **Activity** console surface (load history, live append, reply/continue,
   unread badge). Scheduler fires route here.
3. **Inbox** — SQLite `inbox`, authed `POST /api/inbox`, webhook / sister-agent
   intake, priority tiers, now→Activity fire, dedup + storm guard, `check_inbox`
   tool.

## 7. Related

- [ADR 0001 — Extensibility & Plugin Architecture](/adr/0001-extensibility-and-plugin-architecture)
- [ADR 0002 — Reusable Subagent Workflows](/adr/0002-reusable-subagent-workflows)
- Scheduler: `scheduler/local.py`, `/api/scheduler/jobs`
- A2A streaming: `a2a_handler.py` (`_a2a_rpc`, `_submit_task`, `_stream_new_task`)
