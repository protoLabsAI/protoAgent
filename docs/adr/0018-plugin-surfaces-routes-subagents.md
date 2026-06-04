# ADR 0018 — Plugins contribute surfaces, routes & subagents

- **Status:** Accepted (2026-06-04)
- **Date:** 2026-06-04
- **Deciders:** Josh Mabry; protoAgent maintainers
- **Tags:** extensibility, plugins, surfaces, routes, subagents, fork, architecture
- **Related:** extends [ADR 0001](./0001-extensibility-and-plugin-architecture.md) (plugin system: tools + skills); motivated by the fork-extensibility audit (#505/#506) — surfaces like [ADR 0015](./0015-discord-ingress-surface.md) Discord + [ADR 0017](./0017-google-ui-config.md) Google are wired into `server.py`, which a fork must edit.

> Accepted. Plugins today contribute only **tools + skill dirs**, so the higher-
> value customization axes — an **ingress surface** (a Discord-style gateway), a
> **custom API route**, a **subagent** — still require editing core (`server.py`
> startup, the route registration, `SUBAGENT_REGISTRY`). The fork audit ranked
> this the last re-sync friction point. Extend the `register(registry)` contract
> so a fork drops in a surface/route/subagent as a plugin and never touches core.

## 1. Context & Problem statement

A fork that wants its own ingress (say, a Slack gateway), an extra HTTP endpoint,
or a domain subagent must edit `server.py`'s startup hook, its route wiring, and
`graph/subagents/config.py` — exactly the core files that conflict on every
upstream re-sync. The plugin system already gives a clean, in-process,
opt-in `register(registry)` seam for tools + skills; the remaining axes just need
registry methods + lifecycle wiring.

## 2. Decision

Extend `PluginRegistry` with three contribution types, and split them by
lifecycle (the crux):

1. **`register_router(router, prefix=None)`** — a FastAPI `APIRouter`, mounted
   under a namespaced prefix (default `/plugins/<id>`) at app setup.
2. **`register_surface(start, stop=None, name=None)`** — a lifecycle-managed
   background surface. `start()` (sync or async) is called in the server's
   startup hook (so it has the running loop, like the Discord gateway); `stop()`
   in shutdown. Best-effort: a failing surface logs, never breaks boot.
3. **`register_subagent(config)`** — a `SubagentConfig` added to
   `SUBAGENT_REGISTRY`, picked up by every graph build.

`PluginLoadResult` gains `routers`, `surfaces`, `subagents`.

### Lifecycle: load once at init, not per-reload

The existing code re-runs `load_plugins()` on every config reload (to re-collect
tools). That's fine for tools, but **re-running `register()` would re-mount
routers and re-start surfaces** — illegal (FastAPI routes are fixed after
startup) or messy. So:

- Plugins `register()` runs **once, at process init** (`_main`, before routes +
  app start). All contributions are captured.
- **Routers** mount on the app at init. **Surfaces** register for the startup/
  shutdown lifecycle. **Subagents** register into `SUBAGENT_REGISTRY`. **Tools/
  skills** feed the first graph build.
- A config **reload reuses** the captured tools/skills/subagents (it does not
  re-run plugins). Changing `plugins.enabled` therefore **requires a restart** —
  the same constraint routes already have, documented.

### Trust & security

Plugins are **in-process, trusted, opt-in** (ADR 0001's standing decision) —
untrusted extensions belong on MCP (out-of-process). A plugin router runs with
full server authority; that's acceptable under the same trust model, but:

- Routers mount under `/plugins/<id>` by default so a plugin can't silently
  shadow a core route; a plugin may override the prefix (escape hatch, logged).
- Surfaces + routers are surfaced in `GET /api/runtime/status` plugin meta
  (`routers`, `surfaces` counts) so the operator can see what a plugin wired.

## 3. Consequences

- A fork ships a surface/route/subagent as a `plugins/<id>/` directory — **no
  `server.py` / registry / `SUBAGENT_REGISTRY` edit**, so upstream re-syncs stay
  clean. Closes the last audit friction point.
- The built-in **Discord/Google surfaces could themselves become plugins** later
  (they already match the surface shape: a `start`/`stop` pair). That migration
  is a **follow-up**, not this ADR — v1 ships the extension points + a worked
  example plugin that registers all three.
- `plugins.enabled` changes need a restart (acceptable; routes can't hot-unmount).
- Plugin routes carry full authority — documented; forks own that risk, same as
  any in-process plugin tool.

## 4. Alternatives considered

- **Per-reload plugin re-load with idempotent router/surface registration.**
  Rejected — more complex (track already-mounted ids), and routes still can't
  unmount; load-once is simpler and the restart constraint is honest.
- **A separate `surfaces/` plugin type distinct from `plugins/`.** Rejected —
  one `register(registry)` seam for everything keeps the mental model small
  (ADR 0001's principle); lifecycle is the loader's job, not the author's.
- **Out-of-process surfaces (subprocess/MCP-style).** Deferred — heavier; the
  in-process trusted model fits a fork's own surface. MCP remains the path for
  untrusted code.
