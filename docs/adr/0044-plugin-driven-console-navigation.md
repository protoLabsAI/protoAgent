# 0044 — Plugin-driven console navigation

- Status: Proposed
- Date: 2026-06-09
- Builds on: ADR 0026 (plugin-contributed console surfaces), 0039 (event bus).

## Context

Plugins contribute console views (ADR 0026) that the operator opens from the rail.
Increasingly a plugin — or the **agent**, via a plugin tool — needs to open its view
*programmatically*: e.g. a tool that answers "can you run DOOM?" should focus the DOOM
view and start the game; a board plugin might jump the operator to its board when a run
starts. This is the first concrete step of a broader goal — *the AI navigates the UI for
us*.

The wrong way is for the console to learn each plugin's event (`if topic === "doom.play"
→ open DOOM`). That hardcodes plugin ids into core, doesn't scale, and couples the
console to specific plugins. We need **one general, extensible mechanism**.

## Decision

A reserved host intent **`ui.navigate`** on the event bus, produced by a first-class
plugins API and consumed by a single generic console handler.

- **Producer** — `registry.navigate(view="")` (the plugin registry, alongside `emit`).
  It publishes `ui.navigate` with `{plugin: <this plugin id>, view}`. Unlike `emit`, the
  topic is **not** namespaced — it's a host-level navigation request. It is **scoped**:
  the payload carries the *calling* plugin's id, so a plugin can only ever ask to open
  **its own** views, never hijack the console to another surface.

- **Consumer** — the console subscribes to `ui.navigate` with **one** handler: resolve
  `plugin:<plugin>:<view>` (a blank `view` → that plugin's *first* view), and focus it
  **iff** that surface exists in the live plugin-view set. No per-plugin code; core never
  names a plugin.

```
plugin tool ──registry.navigate("panel")──▶ bus: ui.navigate {plugin:"doom", view:"panel"}
                                                        │
                              console (one generic handler, ADR 0026 surfaces)
                                                        ▼
                              setSurface("plugin:doom:panel")  (if it exists)
```

The DOOM plugin's `can_you_play_doom` tool is the first user: it calls
`registry.navigate("panel")` and the panel's js-dos auto-starts the game.

## Why this shape

- **Extensible** — any plugin gets agent-driven navigation for free by calling
  `navigate()`; core needs no change per plugin.
- **Scoped / safe** — a plugin can only open its own views (id comes from the registry,
  not the payload author), so an enabled plugin can't seize the console.
- **Decoupled** — core knows no plugin ids; plugins don't import core. Same
  no-cross-dependency stance as ADR 0039 events.
- **Graceful** — unknown plugin/view or a disconnected console: the intent is dropped,
  the tool's textual answer still returns.

## Consequences

- One new plugins API (`registry.navigate`) + one general console subscriber. Tiny core
  surface; no plugin-specific branches.
- Fire-and-forget (no ack): a tool can't *know* the operator's console navigated. Fine —
  it's a convenience, not a transaction.
- Future: the intent could grow a `{surface}` form to focus **core** surfaces too (chat,
  activity), gated for the agent rather than per-plugin — a superset of this. Out of scope
  here; the plugin-scoped form is the safe first cut.

## Alternatives rejected

- **Per-plugin events in core** (`if doom.play …`) — hardcodes plugin ids into the
  console. Rejected (the motivation for this ADR).
- **Topic convention `<plugin>.navigate`** consumed via a `*.navigate` pattern — works,
  but overloads the namespaced plugin-event channel for a host concern and is easy to fire
  by accident. A reserved `ui.navigate` intent via an explicit `navigate()` is clearer.
