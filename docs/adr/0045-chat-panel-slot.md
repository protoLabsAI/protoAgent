# 0045 — The chat panel is a slot (swappable chat plugins)

- Status: Accepted (#799)
- Date: 2026-06-10
- Builds on: ADR 0026 (plugin console views), 0038 (two-mode plugin UI), 0039 (event bus), 0032 (plugin middleware).

## Context

The console's chat panel was the one surface no one could replace: `"chat"` was a
hardcoded literal in the surface union, the default-surface fallback, the render
switch, and a specially pinned always-mounted block in `App.tsx`. Plugins could *add*
chat-like panels alongside it, but never *be* the chat — even though the chat module
itself is highly cohesive (six files, two exports consumed by the shell) and the wire
protocol underneath is already a published standard.

We want anyone to ship their own chat panel as a plugin, and we want one canonical
protocol to share with the community. An extensibility spike (2026-06-10) audited the
chat anatomy, the wire contract, and the plugin seams; an antagonistic review then
trimmed the plan to what's load-bearing. This ADR records both the decision and the
deliberate non-decisions.

## Decision

### 1. `chat` becomes a slot

The core rail id `"chat"` stays, but what renders there is resolved at runtime
(`apps/web/src/app/ChatSlot.tsx`), in priority order:

1. **Fork override** — a `src/ext` surface registered with `id: "chat"` (in-process
   React, build-time, trusted; honors `requiresPlugin`).
2. **Plugin claim** — an enabled plugin view declaring **`slot: "chat"`** in its
   manifest (sandboxed iframe, runtime-installed). First enabled claimant wins;
   the claimant gets **no separate rail icon** — it lives under the core chat id.
3. **Built-in default** — the first-party `ChatSurface`. The console is never
   chat-less.

The manifest already passes unknown view keys through (`manifest.py` keeps view dicts
intact), so `slot: "chat"` required **zero backend changes**.

### 2. The slot inherits chat's mount contract

Whatever provides the slot is rendered for the app's **lifetime**; `active` only
toggles visibility. The built-in surface implements this itself; overrides are wrapped
in a `display: contents`/`none` container. This is the #613 invariant generalized:
an unmounted chat loses its in-flight stream.

### 3. The canonical chat-panel protocol is A2A — not a TS SDK

A chat panel's binding contract is the **wire**, which already exists and is public:

| Channel | Contract |
|---|---|
| `POST /a2a` — `SendStreamingMessage` | the streaming turn: text deltas; `application/vnd.protolabs.tool-call-v1+json` DataParts (tool events); `hitl-v1` DataParts (input-required pause → resume by sending the reply on the same `contextId`); cost extension metadata; terminal state |
| `POST /a2a` — `tasks/get` / cancel | durable-task reconciliation + abort |
| `GET /api/chat/commands` | slash-command roster |
| `DELETE /api/chat/sessions/{id}` | retire a session (harvest + purge checkpoints) |
| `POST /api/chat` | non-streaming fallback (one request, full reply) |

Everything is per-agent under the fleet's slug proxy (`/agents/<slug>/…`, ADR 0042);
auth is the `protoagent:init` postMessage handshake (bearer + theme, never in URLs —
ADR 0038), and live extras arrive over the iframe event-bus bridge (ADR 0039). The
extension DataParts are formally defined in the public `protolabs-a2a` package.

We explicitly do **not** adopt the Vercel AI SDK as the core protocol. A2A 1.0 is a
spec we don't have to chase; compat layers (e.g. an AI SDK UI-stream adapter), if ever
built, are adapters — never a second source of truth.

### 4. Conformance checklist (the scar tissue, made transferable)

A replacement chat panel that wants production quality must honor the lessons the
built-in surface paid for:

- **Never unmount mid-turn** — the slot keeps you mounted; don't defeat it by
  remounting your own internals on visibility changes (#613).
- **Reconcile on load** — a message left `streaming` by a reload must self-heal via
  `tasks/get` against the durable task; never spin forever on a stale tab (#615).
- **Request streaming explicitly** and render terminal text **once** — the terminal
  frame replaces accumulated deltas; concatenating both doubles the answer (#616).
- **Scope persistence per agent** — key any local session state by the URL slug, or
  every agent's window shows the same chat (ADR 0042).
- **Surface errors, don't swallow them** — a failed turn must say so inline.

## Deliberate non-decisions (recorded so we don't re-litigate)

The spike's antagonistic review cut these, each **deferred behind explicit demand**:

- **No lifecycle topic taxonomy** (`agent.turn.*`, `agent.tool.*`, …): zero consumers
  today; a topic designed without a consumer is a forever-contract shaped by guesses.
  Design each topic when its first real consumer arrives. (The middleware→bus path
  itself is ready: the bus is thread-safe for middleware emits, and the contribution
  contract is documented in the [Middleware guide](/guides/middleware).)
- **No published chat TS SDK**: iframe panels are arbitrary tech and can't consume a
  React package; the precedent is `packages/plugin-ui`, built for the `ui: react`
  path that ADR 0038 retired. Internal transport refactors happen lazily, unpublished.
- **No AI SDK compat adapter**: no demand signal; revisit if community chat panels
  actually ask for `useChat` compatibility.
- **No `register_checkpointer` hook**: conversation-history swapping has no requester;
  if demand appears, the first move is a `checkpoint.backend:` config selecting among
  LangGraph's *official* savers, and only then a plugin hook (the knowledge store,
  embedder, and middleware are already plugin-swappable — ADR 0031/0032).

## Consequences

- Anyone can ship a chat panel: a manifest `views:` entry with `slot: "chat"` and a
  served page speaking the A2A contract. Forks override in-process via `src/ext`.
- The built-in chat is now just the slot's default provider — a future extraction of
  it into a first-party plugin becomes possible without further shell surgery.
- Known gaps, accepted: an override panel doesn't feed `useAnyChatStreaming` (the
  shell's streaming badge stays off), and bus events from a slot claimant don't light
  a notification dot (its rail id is `chat`, not `plugin:*`). Both are cosmetic;
  promote to requirements only if real panels need them.
- e2e: `apps/web/e2e/chat-slot.spec.ts` pins the contract — claim replaces the
  built-in, no double rail icon, stays mounted across surface switches, and the
  no-claimant default is unchanged.
