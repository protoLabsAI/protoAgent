# 0050 — Background subagents & reactive task notifications

- Status: Accepted
- Date: 2026-06-13
- Builds on: ADR 0003 (reactive agent — event bus, durable Activity thread, inbox),
  ADR 0022 (turn provenance — `origin`/`trigger`/`priority`), ADR 0006 (the A2A terminal
  hook + durable task store), the scheduler's self-POST-to-`/a2a` fire pattern
  (`scheduler/local.py`).

## Context

A live audit of a running instance caught the failure mode this ADR exists to fix. A
SpaceTraders game agent ran a multi-minute research loop — dozens of `web_search` /
`fetch_url` calls — **synchronously inside a single chat turn**. The whole turn blocked: the
console showed one frozen "running" tool card for minutes, the model burned search quota in a
tight loop with no surface to intervene, and nothing could be done in that session until it
returned.

Every subagent path in the agent is synchronous and inline. `task`, `task_batch`, and
`run_subagent` all `await subagent.ainvoke(...)` (`graph/agent.py`), so a delegation blocks
the parent turn's `astream_events` loop for its entire duration. There is no way to fire long
work and keep talking, no visibility into in-flight sub-work, and no notification when a
delegation finishes.

We studied two mature agents that solved this — `protocli` (a qwen-code fork) and Claude Code
v2.18 — and they **independently converged on the same architecture**:

> Fire-and-detach the work → track it in a registry with an exactly-once `notified` flag →
> drain completions back into the model's context as a `<task-notification>` system-reminder
> at the next turn boundary → run two decoupled channels: text-to-the-model (at boundaries)
> and events-to-the-UI (continuously). The launch tool returns immediately with a task id and
> the explicit instruction *"you will be notified — do NOT poll or spawn a duplicate."*

The striking finding is how much of that we already have, just never wired to subagents:

- The **A2A `DatabaseTaskStore`** is a durable task registry with lifecycle states
  (`submitted→working→completed/failed`), a 24h TTL sweep, `GetTask` polling, and restart
  reconciliation — strictly more than the in-memory registries protocli/cc hand-roll.
- The **inbox's `now`/`next`/`later` tiers** are the same priority model as cc-2.18's command
  queue.
- The **scheduler's self-POST-to-`/a2a`** is already an "execute work as a detached, durable
  A2A turn" primitive, complete with `origin` provenance metadata that lands in the Activity
  feed.
- The **event bus** (ADR 0039) is the SSE-wired out-of-band UI channel.
- The **terminal hook → bus → Activity feed** seam (`a2a_impl/executor.py` `set_terminal_hook`
  → `server/a2a.py` `_a2a_terminal`) is the completion-notification path.

The gap is the glue: detached subagent execution, a registry that maps a background job to the
*chat session that spawned it*, a `notified`-gated drain into that session, and a UI surface.

## Decision

**A background subagent is a detached, self-POSTed A2A turn**, tracked in a small durable
registry keyed to its originating chat session, whose completion is surfaced through two
channels: a `notified`-gated `<task-notification>` drained into the originating session's next
turn (model-facing), and a `background.*` event on the bus (UI-facing).

This is delivered in phases; **this ADR ships Phase 1** (the model-facing core) and records the
plan for the rest.

### Why self-POST, not in-process `asyncio.create_task`

The alternative — wrap `_run_subagent` in `asyncio.create_task` with a new in-process registry
— keeps the parent's tool map and context cheaply, but we would have to re-implement durability,
restart reconciliation, lifecycle states, telemetry, and a pollable handle that the A2A task
store already gives a turn for free. Spawning the work as a self-POSTed A2A turn (the scheduler's
proven pattern) inherits all of that, runs the job through the same auth/audit/cost path as any
real caller, and aligns with the agents-as-delegates model (ADR 0042). The cost is that a
background job runs as a **full lead-agent turn in an isolated context**, not a tool-scoped
subagent — see Consequences.

### Phase 1 — background spawn + drain (model-facing core)

1. **`background/store.py` — a durable sqlite registry.** One row per background job:
   `id` (`bg-<uuid12>`), `agent_name`, `origin_session` (the chat session that spawned it),
   `subagent_type`, `description`, `prompt`, `status` (`running|completed|failed`), `result`,
   `notified`, `created_at`, `completed_at`. Instance-scoped under `<root>/background/jobs.db`
   via `scope_leaf` (ADR 0004), mirroring the inbox/scheduler stores. `drain_pending(session)`
   returns completed/failed-but-unnotified rows for a session and flips `notified=1`
   atomically — the exactly-once guarantee. `reconcile_interrupted()` on startup marks any row
   still `running` as `failed` ("did not complete before restart"), since its in-process turn
   died with the process.

2. **`background/manager.py` — the spawner.** Holds the same self-invoke URL + bearer/api-key
   the scheduler derives. `spawn(origin_session, subagent_type, description, prompt)` creates a
   `running` row, then fires a **detached** (`asyncio.create_task`) self-POST to the agent's own
   `/a2a` — `SendMessage`, `contextId = "background:<job_id>"`, `metadata = {origin: "background",
   trigger: job_id}` — and returns the id immediately. A delivery failure (non-2xx / network
   error) marks the row `failed` so it can never stick on `running`.

3. **`run_in_background` on the `task` tool.** When true, the tool captures the originating
   session (`tracing.current_session_id()`), spawns, and returns immediately:
   *"Background agent started: `bg-…`. You will be notified when it completes — do NOT poll,
   check, or spawn a duplicate; continue with other work."* The wording is ported near-verbatim
   from cc-2.18, because the notification system only pays off if the prompt tells the model to
   trust it. Falls back to synchronous execution if the manager is unavailable.

4. **Completion hook.** `_a2a_terminal` recognizes `origin == "background"` (or a
   `background:` context) *before* its `ACTIVITY_CONTEXT` early-return, marks the store row
   complete with the turn's final text, and publishes `background.completed` on the bus (with
   a trimmed `result` so a live console can render it without a refetch). The manager also
   publishes `background.started` on spawn.

5. **Drain-on-next-turn (model-facing channel).** At chat turn-input assembly
   (`server/chat.py`), a fresh user turn prepends a `<task-notification>` message for each
   completed-but-unnotified job of that session before the user's message — so it enters the
   graph input, is checkpointed into history, and the model sees it for the whole turn (and
   thereafter). Exactly-once via the `notified` flag.

6. **Live delivery to an open chat (human-facing channel).** The two channels are decoupled:
   the model learns at the next turn boundary (step 5), but a human watching the spawning chat
   shouldn't have to send a message to see the result. A `BackgroundWatch` component subscribes
   to `background.{started,completed}` on the event bus (already SSE-wired, scoped to the
   window's agent) and, when an event's `origin_session` matches an **open** session in this
   window, injects a `system` message with the result into that session's transcript + a toast
   (and an OS notification if the tab is hidden). The injected message is **display-only** —
   the backend owns conversation history, so it never double-feeds the model. (Pulled forward
   from the Phase 3 plan because "the spawning chat, if still open, gets the update live" is
   the core of the reactive experience.)

### Delegation steering (so the agent actually uses this)

A capability the model won't reach for is wasted. The same audit episode showed the lead
agent doing a heavy SpaceTraders subagent's job (web-research for ship data) **inline and
synchronously** — the strategist subagent (40 turns, `web_search`/`fetch_url`/`st_docs`)
exists and the agent knows about it (the system prompt lists the whole runtime registry),
but a long subagent run today blocks the turn and burns quota in the foreground. Three
nudges close that:

1. **Enum the `subagent_type` arg.** The `task` tool renders `subagent_type` as a
   JSON-schema `enum` of the live registry (plugin-contributed subagents included — it's
   built after registration, rebuilt on reload). The model can no longer fat-finger a name,
   and the valid roster is visible in the tool schema itself.
2. **Stronger routing.** The system prompt's delegation section now tells the lead to match
   work to the most specialized subagent whose description fits and **not to grind
   domain work (deep research, strategy, multi-step gathering) inline** when a subagent is
   purpose-built for it.
3. **Background-by-default for heavy subagents.** The same section tells the lead to default
   to `run_in_background=true` for long / independent / quota-heavy delegations (a strategic
   audit is the textbook case). The guidance is unconditional so the system prompt stays a
   turn-stable cache prefix (shared by the live graph, the cache warmer, and the native
   loop); the `task` tool always accepts the arg and degrades to synchronous if the manager
   is disabled.

### Phase 2 — reactivity / idle-wake (shipped)

A finished background job now **wakes the agent autonomously**, so it acts on the result
without waiting for the spawning session's next turn. On completion, `_handle_background_terminal`
adds a `now`-priority **inbox item** (the existing reactive path, ADR 0003) whose fire runs a
turn into the **Activity thread** — storm-guarded by the inbox's `StormGuard`. Activity is the
right home (not the originating chat): the console's chat view is localStorage-driven and won't
render a backend-initiated turn, but the **Activity feed is server-driven** (it renders the
`activity.message` bus event), so the autonomous response shows up there live. The wake stimulus
names the originating session so the agent can reference it. Gated by `BACKGROUND_WAKE` (on by
default; `=0` opts out, parity with `BACKGROUND_DISABLED`). `background.started` is published on
spawn (Phase 1); `background.progress` remains a follow-up.

### Phase 3 — chat UX (shipped, except the live progress card)

A `BackgroundJobs` widget in the console's `UtilityBar`: a **pill** that shows a spinner +
running count while jobs are in flight and an **unread dot** when jobs finish, and a
**dialog** listing each job (status icon, subagent + description, live elapsed, and the
result rendered as markdown for finished jobs — expandable). It hydrates from
`GET /api/background`, then tracks live off the `background.{started,completed}` bus events;
opening the dialog clears the unread count. Completion toasts already shipped in Phase 1
(`BackgroundWatch`). Read-only — stop/kill is Phase 4.

**Deferred:** the *rich live subagent card in-transcript* (a per-tool progress feed like
protocli's `AgentExecutionDisplay`). It needs a `background.progress` channel — background
jobs run as detached A2A turns whose tool-by-tool frames aren't surfaced to the bus today.
That progress channel + card is a follow-up. (`@protolabsai/ui/ai`'s ToolCall component is
the thing to check first when it lands.)

### Phase 4 (planned, not in this ADR's PR)
- **Phase 4 — control + the runaway fix.** `task_output(id, block, timeout)` and `stop_task(id)`
  tools (cc-2.18's TaskOutput/TaskStop), and foreground→auto-background on a time budget so a
  long synchronous delegation transparently detaches and becomes killable — the direct cure for
  the audited incident.

### Follow-up — background batch + concurrency cap (shipped)
- **`task_batch(run_in_background=True)`** fans a whole batch out detached: every spec spawns as
  its own background job and the call returns immediately with the job ids, instead of blocking
  until all finish (the foreground `task_batch` path). It's the multi-task analog of
  `task(run_in_background=True)` and goes through the same `BackgroundManager.spawn`; each
  completion drains back into the spawning session independently — one task-notification per job —
  reusing Phase 1's exactly-once drain. Bad specs (missing prompt / unknown subagent) are skipped
  inline; the good ones still spawn. With no manager configured it degrades to the foreground batch.
- **Concurrency cap (the missing backpressure).** `BackgroundManager` now bounds concurrent
  background turns with a semaphore that gates the self-POST in `_fire` (held for the whole turn).
  A fan-out can no longer open one full lead-graph turn per job against the gateway at once; jobs
  past the cap queue at the semaphore. Default 3, override `BACKGROUND_MAX_CONCURRENCY`. It applies
  to *every* spawn — so several ad-hoc `task(run_in_background=True)` calls are bounded too. A
  queued job's row reads `running` until a slot frees (it is accepted); `cancel` and
  `reconcile_interrupted` both already handle a row whose turn hasn't fired yet.

### Follow-up — deterministic work jobs (`spawn_work`, shipped)

Phase 1 assumed every background job is an **LLM subagent turn** (§"Why self-POST"). But some
long work is *deterministic* — a media-ingestion pipeline (fetch → transcribe → chunk → embed)
is a fixed sequence of calls, not a reasoning task. Routing it through a self-POSTed lead-agent
turn would spend model tokens + latency + nondeterminism just to invoke one pipeline. So the
manager gains a second, non-turn spawn path:

- **`BackgroundManager.spawn_work(origin_session, kind, description, work, detail)`** runs a plain
  zero-arg coroutine `work()` as `asyncio.create_task`, under the **same** concurrency semaphore
  as background turns. It reuses the durable `BackgroundStore` verbatim (`kind` is stored as
  `subagent_type`; a work job simply has no `a2a_task_id`), so the exactly-once drain, restart
  reconciliation, and `list`/`get`/`clear` all work unchanged.
- **This is the deliberate exception to "self-POST, not `asyncio.create_task"** (§Decision). That
  rationale holds for *turns* — they need the A2A task store's lifecycle/telemetry/pollable handle.
  A deterministic job needs none of that machinery; it needs the *registry + notification*, which
  the store already provides. So `create_task` is correct here precisely because there is no turn.
- **Completion parity.** No A2A turn fires, so `_a2a_terminal` never runs for a work job. `_run_work`
  therefore settles the row (`mark_complete`), publishes `background.completed` with the **same
  payload** the terminal hook emits (so the console card is identical), and calls an injected
  `on_terminal(job)` hook — which the server wires to the **same `_spawn_background_wake`** the turn
  path uses (ADR 0003 idle-wake, gated by `BACKGROUND_WAKE`). All three completion channels
  (live card, next-turn drain, autonomous wake) fire identically for a work job.
- **Cancel.** A work job has no `a2a_task_id`; `cancel` stops its `asyncio.Task` directly and settles
  the row (no `CancelTask` round-trip).

**First consumer — `knowledge_ingest` (ADR 0031/ingestion).** The agent-facing ingest tool detaches
any slow source — a URL fetch (web/YouTube) or media transcription (audio/video) — as a `spawn_work`
job so it never blocks the chat turn; only a small local text/Markdown file (≤64 KB) ingests inline.
With no manager wired it degrades to inline (blocking, but correct). This is the durable, non-blocking
answer to "hand the agent a YouTube link / an `.mp4`" — the field-standard *return-handle → detached
worker → reactive completion* shape (A2A task lifecycle, Microsoft Agent Framework background
responses, classic job-queue-with-notify), reusing machinery this ADR already built.

## Consequences

- **The chat stays live.** A delegation marked `run_in_background` returns in milliseconds; the
  session keeps streaming and the user keeps talking while the work runs as its own durable A2A
  task.
- **Background jobs run as full lead-agent turns**, not tool-scoped subagents. They get the full
  toolset and a fresh, isolated context (good: no parent-context pollution; capable). Per-job
  tool-allowlist scoping (running the actual `researcher` allowlist) is deferred — the
  `subagent_type` currently contributes a role preamble to the fired prompt, not a tool fence.
- **Three ways a completion surfaces.** (a) The human sees it live in the open spawning chat
  (`BackgroundWatch`, step 6); (b) the model ingests it on that session's next turn (the drain,
  step 5); and (c) the agent **wakes and acts on it autonomously** in the Activity thread
  (Phase 2). (a)+(b) are chat-scoped and human-paced; (c) is the self-driving path that fires
  even if the user never returns to the chat.
- **Autonomous wake costs a turn per completion.** Each finished background job fires one
  Activity turn (storm-guarded). Proportionate — background jobs are deliberate and low-volume —
  but `BACKGROUND_WAKE=0` disables it for cost-sensitive or non-reactive deployments.
- **A background turn could itself spawn background turns.** Bounded in practice by focused
  prompts + the prompt contract; a hard depth fence is a later refinement.
- **Background fan-out is bounded.** A `task_batch(run_in_background=True)` — or many single
  `task(run_in_background=True)` calls — can't swamp the gateway: at most `BACKGROUND_MAX_CONCURRENCY`
  turns run at once, the rest queue. Trade-off: a queued job reports `running` before its turn
  actually starts (a small cosmetic lie the console's running-count inherits).
- **Long jobs vs. the self-POST timeout.** `_fire` holds the connection open for the whole turn
  (like the scheduler); a job exceeding the fire timeout is marked `failed` even if still
  running. The timeout default is generous and configurable; a true submit-and-detach (A2A
  streaming) is a later refinement.
- **Restart leaves no zombies.** `reconcile_interrupted()` fails orphaned `running` rows on boot.

## References

- The audit incident and the cross-agent study that motivated this (protocli `backgroundShells/`
  + `tools/agent.ts` drain-on-next-turn; Claude Code v2.18 `Task.ts` + `messageQueueManager.ts`
  priority queue + `useQueueProcessor.ts` idle-wake).
- `scheduler/local.py` `_fire` — the self-POST-to-`/a2a` pattern this reuses.
- `a2a_impl/executor.py` `TurnOutcome` / `set_terminal_hook`; `server/a2a.py` `_a2a_terminal`.
