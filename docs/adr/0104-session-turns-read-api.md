# 0104 — The session turns read API: durable chat history has a server-side reader

Status: **Accepted** (Swap & Resume S5; umbrella: the swap-resume initiative)

## Context

A chat session's rendered history lives in exactly one place: the browser's
`localStorage`, namespaced per agent slug. The server holds three durable
records — the A2A task store (every turn's status, artifacts, and per-frame
history), the LangGraph checkpoint (the model-facing thread), and the
trajectory (ADR 0102, refs not text) — and **none of them had a read API shaped
like "this session's turns."** Consequences, found by the swap-resume audit:

- A second device (or a fresh browser profile) sees an empty chat for a session
  the server knows everything about.
- The reattach path (S1) could replay a single task by id, but "what happened
  in this session while I was away" had no answer better than the one stuck
  message's task.
- Export (`/export`) renders Markdown for humans; nothing serves the
  console-shaped wire (status/artifacts/history) a client can actually replay.

## Decision

One bearer-gated route on the operator API:

```
GET /api/chat/sessions/{session_id}/turns?limit=50
```

It reads the **A2A task store** (the SDK's `tasks` table — turns are keyed by
`context_id`, which IS the console session id) via the engine the server
already exposes (`STATE.a2a_task_engine`), ordered by `last_updated`, and
returns each turn's raw wire pieces plus conveniences:

```json
{"turns": [{"task_id", "state", "last_updated", "text", "status", "artifacts", "history"}]}
```

`status`/`artifacts`/`history` are the SDK's stored JSON, untransformed — the
same shapes the console's A2A frame dispatcher already decodes, so a client
replays a turn through the exact code path the live stream uses (no second
mapping to drift). `text` is the joined artifact text for cheap consumers.

Deliberately NOT in this slice: console adoption as the source of truth.
localStorage stays the primary store (it holds client-side niceties the task
store doesn't — ordered parts, per-message usage pins); this API is the
recovery/second-device substrate, consumed opportunistically.

## Consequences

- "This session's messages, including the in-flight turn" is finally a server
  answer — the in-flight task appears with its accumulated artifacts/history
  and a non-terminal state.
- Multi-device catch-up becomes buildable without protocol work.
- The task store's retention now matters to history depth (it already persists
  every turn; a future retention policy must consider this reader).

## Rejected alternatives

- **Reading the LangGraph checkpoint** — model-facing (context frames,
  compaction stubs), not operator-facing; export_session already covers the
  human-readable need.
- **A new chat-history store** — a fourth durable record to keep consistent;
  the task store already has everything the wire needs.
- **Replaying via the events bus** — `chat.progress` is origin-gated and
  unretained by design (double-render prevention); the bus is a live channel,
  not a history store.
