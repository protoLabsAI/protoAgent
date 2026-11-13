# ADR 0130 — Server-authoritative live sync: every client follows the server, nothing is guessed locally

- Status: Accepted (2026-11-12, Josh: "yes as suggested")
- Date: 2026-11-12
- Deciders: Josh Mabry; protoAgent maintainers
- Implementation: not started. Slices S0–S5 below.
- Builds on: [ADR 0104](./0104-session-turns-read-api.md) (durable session turns are the transcript of record), the event bus (`src/events/bus.py`). Supersedes the console's local turn-liveness inference (`apps/web/src/chat/sessionLiveness.ts`, the reattach races in `chat/reattach.ts`) once S4 lands.

## Context

Josh, 2026-11-12: "we need our clients to all update automatically from the server so that all devices stay in sync at all times."

In one afternoon, three bugs came from one cause: the console decides locally what a session is doing.

1. A `board_show_cards` turn finished while Josh was on another agent. On return, the desktop reattach swallowed a JSON-RPC rejection and kept a half-drawn bubble, so the cards never rendered (#5161).
2. A server-started turn (a background delegation's result) finished while the chat was hidden. A placeholder bubble kept spinning beside the real answer (bd-wk4ti).
3. Because of (2), the console believed a turn was live and sent Josh's next message as a steer. The server had no live turn, so the steer sat pending forever and Josh couldn't chat until a manual fix (bd-0ad5v).

Earlier, phone and desktop showed different chat histories (#4970, #5066).

What exists today:

- **The event bus** (`events/bus.py`) is in-process with a global `seq`. Its replay ring holds the last **128** events, and only in memory, so a client away for a busy minute or across a restart can't catch up. Progress frames are `retain=False` and never replay.
- **The console** keeps its own picture of each session. It holds the transcript in IndexedDB, uses `streaming` status from local producers (local turns, reattach, boot inference), and runs a reconciler (`sessionLiveness.ts`) that guesses when to hand a session back to idle. Every producer that ends without settling, or races another, leaves the UI stuck, and each fix so far has patched one producer.
- **Clients** catch up on reconnect and visibility, but only for what the ring still holds. Agent switches, hidden desktop panes and phones that sleep all depend on that best effort.

## Decision

### D1 — The server owns session state, and clients render it

Each chat session has a **server-held state**: `{live_task_id, status: idle|running|paused, last_change_seq, title, updated_at}`. The status is derived on the server from the task store and turn lock, never from a client.

The console's composer lock, Stop button, spinners and "queued" steering derive from this state, not from local producers. A client may draw its own send optimistically, keyed by `messageId` and `taskId`. The server's state replaces the optimistic draw as soon as it arrives.

### D2 — A durable, resumable change feed

The server keeps a **durable change log** per agent instance: an append-only SQLite table of `(seq, kind, entity_id, ts)`. It records every change a client renders:

- session state transitions;
- turn started, completed, failed and paused;
- a turn's durable parts (text and components), at settle granularity, not token streaming;
- decisions;
- Work items: jobs, follow-ups, instructions and tasks;
- inbox and feed rows;
- sessions created, renamed and deleted.

Progress frames stay live-only, as today.

- **Live:** SSE `/api/events` sends `id: <seq>`. A client resumes with `Last-Event-ID` (or `?since=`). The server replays from the log, not from a 128-entry ring.
- **Gap:** if a client's cursor is older than retention (default 7 days or 100k entries), the server answers `resync_required`. The client then refetches the snapshots it holds: session list and open sessions' turns. It never guesses.
- **Entity fetches:** events carry identities, not payloads. A client reacts by fetching the entity's current server state (`GET …/turns?since_seq=`, `GET /api/decisions/{id}`, …). Order is guaranteed by `seq`, and a duplicate or missed event is harmless.

### D3 — Every client converges, always

Desktop windows, hidden panes, other-agent views, phones and extra tabs all subscribe the same way. On each of these:

- reconnect,
- the page becoming visible,
- switching to an agent,
- the app resuming from sleep,

a client resumes from its cursor, and as a backstop compares its open sessions' `last_change_seq` to the server's. A client never holds a "streaming" bubble the server doesn't report as live.

### D4 — Sync is tested as a contract

Multi-client end-to-end tests drive two browser contexts against one server:

- A change in one (send, answer a decision, background result, agent-started turn) appears in the other within 2s, with no reload.
- Network drop → reconnect, hidden → visible, and a server restart mid-turn all converge to the server's state.
- A client never shows a spinner, a queued steer or a locked composer for a session the server reports idle.

These run in CI on every console and server change.

### D5 — Retire the local guesswork

Once D1–D4 hold, the console stops inferring liveness:

- `sessionLiveness.ts` reduces to "render server state";
- `reattach.ts` reduces to "subscribe to the live task the server names";
- a steer goes to the server, which decides whether to steer or start a turn (bd-0ad5v).

Every stuck-state bug of this class becomes a server-state bug with one place to fix it.

## Consequences

- **Good:** phone, desktop and every pane show the same thing. Stuck spinners, phantom queues and missing components become impossible by construction, not patched per producer. A restart loses nothing a client needs.
- **Cost:** a durable change-log table and its retention sweep; one SSE id and resume path; a console refactor of session status. Snapshots are re-fetched more often after gaps, which is cheap because turns are paged.
- **Risk:** the S4 refactor touches the chat surface broadly. It lands behind the D4 contract tests, built first in S0.

## Slices

- **S0:** the D4 multi-client contract tests, written first. They fail on today's main for the three bugs above. These are the acceptance bar for every later slice.
- **S1:** the durable change log, `seq` on SSE, `Last-Event-ID` resume, and `resync_required`. Server only.
- **S2:** server-held session state (D1) on `GET /api/chat/sessions` and as `session.changed` events.
- **S3:** the console sync client: cursor persistence, resume, visibility, agent-switch and sleep hooks, and entity refetch.
- **S4:** the console renders session status from server state (D1). Remove local liveness inference (D5).
- **S5:** Work, decisions, inbox and Feed consumers move onto the same feed, replacing their per-surface polling and invalidation.

Related fixes already on the board stay valid and ship first: #5161 (desktop reattach rejection), bd-wk4ti (one bubble per task) and bd-0ad5v (a steer with no live turn starts a turn).
