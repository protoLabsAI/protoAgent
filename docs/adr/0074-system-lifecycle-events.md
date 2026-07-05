# 0074 — System lifecycle events

- Status: Accepted
- Date: 2026-07-04
- Builds on: ADR 0039 (the plugin event bus), ADR 0028 (plugin goal hooks — the seam this
  mirrors), ADR 0003/0053 (scheduler / `run_in_session` — the reaction primitive).

## Context

The event bus (ADR 0039) lets plugins broadcast and subscribe, but every topic today is a
*plugin* event (`artifact.created`, `<plugin>.<thing>`). Nothing broadcasts the **system's own
lifecycle** — that the process finished booting, that the agent just went from idle to active,
that the desktop shell woke from sleep. An operator who wants "when the agent boots, DM me a
summary" or "when it wakes up, catch up on what happened while it slept" has no seam: they'd have
to fork `server/` and hand-wire an emit.

Three lifecycle transitions are worth a first-class, stable contract:

- **`app.loaded`** — boot finished: the graph is compiled and the scheduler, surfaces, and
  fleet-autostart members are up. The "I'm ready" beacon.
- **`agent.active`** — the agent went **idle → active** (a turn started after a quiet gap). Not
  *every* turn (that fires constantly) — the leading edge of a work session.
- **`system.wake`** — the desktop shell woke / regained focus. **Reserved** in this ADR: the
  bus/seam/config accept it now; the Tauri emit (Focused → `POST /api/events/publish`) is a
  deliberate follow-up so this PR carries no desktop work.

## Decision

Add a **system lifecycle** layer over the ADR 0039 bus with three coordinated surfaces, all
**opt-in** and all **error-isolated** (a bad hook / webhook / prompt can never break boot or a
turn).

### D1 — A dispatcher in `graph/lifecycle/` (`fire(event, payload)`)

One entry point every server emit site calls. It does three things per event:

1. **Broadcasts** the event on the bus (ADR 0039) under a **dot-namespaced** topic
   (`app.loaded`, `agent.active`, `system.wake`) — so any plugin/console subscriber reacts with
   zero wiring.
2. **Fires plugin hooks** (D2).
3. **Runs config reactions** (D3).

It lives in `graph/` and imports only `graph.sdk` / `graph.plugins.host` / `runtime` (via the
SDK) / `httpx` — **never `server` or `operator_api`** (the import-layering contract). The config
event name (`app_loaded`) → bus topic (`app.loaded`) mapping lives in exactly one place
(`graph/lifecycle/dispatch.py:TOPICS`).

### D2 — A plugin hook seam (`register_lifecycle_hook`)

Mirrors the goal-hook seam (ADR 0028 D4) exactly. A plugin calls
`registry.register_lifecycle_hook(on_app_loaded=…, on_agent_active=…, on_system_wake=…)`; the
loader collects the hooks (`PluginLoadResult.lifecycle_hooks`) and the server installs them into
the live module registry (`graph/lifecycle/hooks.py`, re-set on config reload alongside the goal /
watch hooks). Each callback takes the event **payload** dict, may be sync or async, and a raising
hook is logged + swallowed. (A hook is the direct-callback form; `registry.on("app.loaded", …)` is
the zero-config bus alternative.)

### D3 — An operator-facing config reaction path (`lifecycle_hooks`)

A top-level `lifecycle_hooks:` list in `langgraph-config.yaml`, each entry
`{event, prompt?, webhook?, session?}`:

- `event` — `app_loaded` | `agent_active` | `system_wake`.
- `prompt` — enqueue a follow-up agent turn via `sdk.run_in_session` (ADR 0053) in `session` (or,
  for `agent_active`, the event's own session).
- `webhook` — POST `{event, data}` to a URL (async, short timeout).

**Default empty ⇒ nothing fires** beyond the bus broadcast — opt-in by construction. Being a
list-of-dicts (not a scalar), it round-trips through `config_io.py` §B like `filesystem.projects` /
`mcp.servers`, not the string-typed settings `FIELDS`.

### D4 — Payloads carry a timestamp + previous state

Every payload carries `ts` (epoch) and `previous_state`. `agent.active` also carries `session_id`
and `idle_seconds`; `app.loaded` carries `agent` + `port`. `agent.active` is **debounced** by a
pure, unit-testable helper (`should_emit_active(now, last_activity_ts, threshold=300s)`): emit on
the first turn since boot (`previous_state="boot"`) or the first turn after ≥ threshold idle
(`previous_state="idle"`), suppress otherwise. The last-activity timestamp lives on
`runtime.state.STATE`.

### D5 — A read-only `/lifecycle` chat command

A reserved core command (like `/goal`) that lists the three events and the currently-configured
config reactions + registered plugin hooks. **Listing only** for v1 — the config file is the
source of truth; no runtime mutation.

## Consequences

- A new **event contract** (three system topics) that forks and plugins can rely on. This ADR is
  the reference for their names + payloads.
- Plugins get system-awareness (`register_lifecycle_hook` **or** a bus subscription) and operators
  get a no-fork reaction path — both without touching `server/`.
- `system.wake` is reserved but inert until the desktop emit ships; emitting it is purely additive
  (the seam already accepts it).
- This is a small, additive extension of ADR 0039 (a new *producer* of system events on the same
  bus), plus the hook/config seams goal mode already established (ADR 0028) — no new subsystem.
