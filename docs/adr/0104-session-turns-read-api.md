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

Two bearer-gated routes on the operator API:

```
GET /api/chat/sessions?limit=50
GET /api/chat/sessions/{session_id}/turns?limit=50
```

The bounded session index returns newest activity first as
`{"sessions": [{"session_id", "last_updated", "turn_count"}]}`. Discovery is
necessary because a fresh browser has no local session ids with which to call
the turns reader. It deliberately carries no transcript content; the console
only fetches turns for server-only or locally empty sessions.
Only `chat-` contexts are indexed: the same task store also contains Activity,
delegation, Fleet Room, and API contexts, none of which are console chat tabs.

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

The console consumes the index opportunistically at boot. `localStorage` stays
the primary store (it holds client-side niceties the task store doesn't —
ordered parts, per-message usage pins): a non-empty local session always wins,
while a missing or empty one is rebuilt from durable turns through the same
A2A reducers used by live and reattached turns. Fetches are bounded and a
failed member/read leaves the local console usable. Each read captures the
eligible local session object; delete, clear, send, rename, or another local
edit before commit changes/removes that object and vetoes the stale result.
For a nonterminal turn hydration keeps the latest durable partial visible as a
fallback. It marks those replay-derived fields; every authoritative full Task
snapshot resets them immediately before replay. This matters when a subscription
emits its snapshot and then fails before a retry/GetTask emits the same snapshot:
reasoning/components/tools still apply exactly once, while a failed/cold
reattach that never receives a Task frame does not leave a blank bubble.

Explicit session retirement also removes that session's A2A task rows. Without
this invariant, the discovery route would resurrect a deliberately deleted tab
until the task store's normal TTL elapsed. Retirement also writes a durable,
non-evicting tombstone in the task database: an in-flight producer may save its row
again after deletion, and the index/turn reader must continue excluding that
late save. Count/age eviction is unsafe without a proven producer-liveness
bound. Clear-history uses `retire=false` because it keeps the tab/id alive; the
console disallows it while either a local or server-initiated turn is known to
be active because a reusable id has no tombstone protection against a
post-clear save. This is still a client-side liveness check: without a server
generation/compare-and-delete primitive, a producer can theoretically start in
the interval between that check and the delete request.

Closing an active goal with **Keep running** is neither clear nor retirement.
The console persists that session id in a separate local-dismissal set and
excludes it from boot hydration, so reload does not reopen a deliberately
detached goal tab. A future explicit **Open chat** action must first clear that
dismissal (`restoreDismissedSession`) and then hydrate the same durable id.

The auxiliary tombstone table is created lazily through `CREATE TABLE IF NOT
EXISTS` on the first index/turn read or retirement. Those nominal GET paths can
therefore perform a one-time schema write on an older database; subsequent
reads are ordinary queries.

## Consequences

- "This session's messages, including the in-flight turn" is finally a server
  answer — the in-flight task appears with its accumulated artifacts/history
  and a non-terminal state.
- Multi-device catch-up becomes buildable without protocol work.
- A fresh console discovers recent server-known sessions without downloading
  every transcript up front, and explicit deletion cannot resurrect them.
- Durable deletion intent grows with the number of retired session ids. Safe
  compaction requires a future proof that no producer can save those ids again.
- Recovery reaches only as far back as the A2A task store's current 24-hour
  terminal-task retention. The task store already persists every turn; changing
  that TTL is therefore also a user-visible history-depth decision.

## Rejected alternatives

- **Reading the LangGraph checkpoint** — model-facing (context frames,
  compaction stubs), not operator-facing; export_session already covers the
  human-readable need.
- **A new chat-history store** — a fourth durable record to keep consistent;
  the task store already has everything the wire needs.
- **Replaying via the events bus** — `chat.progress` is origin-gated and
  unretained by design (double-render prevention); the bus is a live channel,
  not a history store.
