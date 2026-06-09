# 0039 — A plugin event bus (decoupled pub/sub, no cross-plugin dependencies)

- Status: Accepted
- Date: 2026-06-08
- Supersedes: the per-plugin "badge poll" notification approach floated during the ADR 0038 work.
- Builds on: ADR 0003 (the reactive-agent event bus), ADR 0026/0038 (plugin console surfaces as
  sandboxed iframes), ADR 0027 (git-installable plugins).

## Context

Plugins need to react to things — "an artifact was rendered, light the rail icon"; "the agent wrote
a note, refresh the editor". The naive fix (each plugin exposes a `/badge` endpoint the console
polls) is bespoke, chatty, and only solves notifications. The deeper need, in an ecosystem where
third-party plugins and forks coexist, is a way for components to **broadcast events and forget**,
and for any other component to **subscribe to what it cares about — without ever depending on the
plugin that emitted it**.

We already have the spine: ADR 0003's `events/bus.py` is an in-process, fire-and-forget pub/sub that
fans events out to every console over `GET /api/events` (SSE). But it is **server→client only**,
**untyped** (no topic filtering — every subscriber gets everything), and **nothing in-process can
subscribe** (only the SSE consoles consume it).

[WorkStacean](https://github.com/bioshazard/workstacean) is the reference for the missing pieces: a
hierarchical, dot-namespaced topic bus with `*`/`#` wildcards; in-process handler subscriptions; and
a plugin contract (`install(bus)`) where *"plugins know only the EventBus contract, not each other"*.

## Decision

Promote ADR 0003's bus into a **plugin event bus** — one server-authoritative bus, extended along
three tiers. (Topology, enforcement, and persistence were decided explicitly; see Options.)

### Tier 1 — Server bus (the source of truth)

Extend `EventBus`:
- **Topics + wildcards.** Events carry a dot-namespaced topic (`<plugin>.<event>`, e.g.
  `artifact.created`). Subscriptions match by topic with `*` (one segment) and `#` (tail) wildcards.
- **In-process handler subscriptions** alongside the existing SSE fan-out: `subscribe_handler(topic,
  handler)` lets server-side (Python) plugins react in-process. The SSE path is unchanged — every
  console still receives the stream and filters client-side.
- **Ring buffer.** A small bounded ring of recent events so a reconnecting console can catch up
  (`GET /api/events?since=<seq>`); **no durable event log** (see Persistence).

Plugins reach it through the registry (already wired via `HOST`):
```python
def register(registry):
    registry.emit("artifact.created", {"id": "..."})      # publish (namespace-guarded)
    registry.on("notes.*", handler)                        # subscribe in-process
```

### Tier 2 — Client + iframe relay

- The console already consumes `/api/events`; it becomes a small **client dispatcher**.
- Sandboxed plugin iframes join via the existing ADR 0026 bridge: the host relays matching events in
  (`postMessage {type:"protoagent:event", topic, data}`) and accepts publishes back
  (`{type:"protoagent:publish", topic, data}` → `POST /api/events/publish`). One bus, the browser is
  a mirror; client publishes go *up* to the server then fan back out.

### Tier 3 — The contract (the no-cross-dependency clause)

**The bus is the only inter-plugin channel. Plugins never import one another.** Enforced
structurally, not by etiquette:
- **Namespacing + a light runtime guard:** a plugin may publish only under its own namespace
  (`<plugin_id>.*`) — the registry/route stamp and reject otherwise. Subscribing is read-only and may
  match any topic.
- **Discoverability:** manifests *may* declare `emits:` / `subscribes:` (documentation + future
  tooling), but undeclared subscriptions are allowed — the guard is on *publishing*, which is the
  only direction that can spoof or spam.

This maps onto the ADR 0038 threat model: **subscribe is safe; publish is the risky direction**, so
publish is the gated one (namespace-stamped, rate-limited for iframes).

## First consumers (proof, not scope creep)

- **Rail-icon notification dots (core).** The console subscribes; an event under `<pluginId>.*` while
  that surface isn't open ⇒ a dot on its rail icon, cleared on open. No badge endpoints, no polling.
- **artifact-plugin v0.2.0.** `show_artifact` emits `artifact.created` ⇒ the dot lights even when the
  panel is closed. (History/download are local plugin features, unrelated to the bus.)

## Options considered

**Topology** — *(chosen)* extend the one server bus, server-authoritative; vs a client-only UI bus
(two disjoint buses, the agent can't emit UI events) vs two bridged buses (most moving parts). One
bus keeps a single source of truth and reuses ADR 0003.

**Enforcement** — *(chosen)* namespacing + light guard (publish to your own namespace; subscribe
anything); vs a strict declared contract (manifest must declare emits/subscribes; runtime rejects
undeclared — more ceremony) vs convention-only (no guard — a plugin could publish under another's
namespace).

**Persistence** — *(chosen)* ephemeral fan-out + a small in-memory ring buffer for reconnect
catch-up; the Activity thread (ADR 0003) already persists agent-facing events. Vs a SQLite event log
on `#` (like WorkStacean) for full replay/audit — deferred (storage + retention cost; revisit if we
need cross-session replay or a debugging timeline).

## Consequences

- A clean, decoupled extension primitive: plugins broadcast and forget; consumers subscribe to what
  they want; nobody imports anybody. Notifications stop being bespoke.
- The bus payload gains a topic field; the SSE shape stays back-compatible (the existing `event`
  string *is* the topic).
- New surface to secure: client/iframe publishing. Gated by bearer + namespace stamp + rate limit.
- Slices: (1) server bus extension + registry `emit`/`on` + `POST /api/events/publish` + tests;
  (2) client dispatcher + iframe relay + the notification dot (held draft — UI local-test gate);
  (3) `artifact-plugin` v0.2.0 emits `artifact.created` + history + download.
