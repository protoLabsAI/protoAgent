# 0043 — Plugin consumption SDK + workflows as the first opt-in core extraction

- Status: Proposed
- Date: 2026-06-09
- Builds on: ADR 0001 (extensibility), 0002 (workflows), 0026/0027 (plugins),
  0038 (two-mode plugin UI + `src/ext` seam), 0009 (control stack)

## Context

Plugins so far only **contribute** to the host — `PluginRegistry.register_*` adds tools,
routers, recipe dirs, goal verifiers, middleware. None had to **consume** deep core
capability. Workflows (ADR 0002) is the first feature where that asymmetry bites: it's a
real subsystem (engine + registry + tools + an operator API + the Studio surface) that we
want **out of the default build** (lean core, like GitHub → plugin), but its engine runs
each step as a **subagent** — so as a plugin it must call back into core's subagent
executor. There was no stable way to do that without importing `graph.agent` internals,
which would couple the plugin to core's refactors.

Two needs, then: (1) a **stable consumption surface** plugins call into core; (2) prove it
by extracting workflows onto it and shipping workflows off-by-default.

## Decision

**1. A plugin SDK with two explicit halves.**

- **Contribution** — `PluginRegistry.register_*` (unchanged): what a plugin *adds*.
- **Consumption** — new `graph/sdk.py`: what a plugin *calls back into core*. Plugins
  import `from graph.sdk import …`, never `graph.agent` / `runtime.state` internals, so
  core can refactor underneath them.

v1 is deliberately tiny — only what workflows needs:
`run_subagent(subagent_type, prompt, …)` (wraps the existing `run_manual_subagent`, pulls
config/stores from runtime state), `subagent_types()`, and `config()`. Grow it
deliberately as more plugins tap core; this is the seam we lean on going forward.

**2. Workflows becomes an opt-in plugin (`plugins/workflows`, `enabled: false`).**

- The engine + registry move into the plugin (standalone — the engine was already
  decoupled from the runner via an injected `run_step` callback). It injects
  `sdk.run_subagent` as that callback — the first real SDK consumer.
- Tools (`run_workflow`, `save_workflow`) register via `register_tools`; the operator API
  is the plugin's own `/api/plugins/workflows` router (validation errors → 400).
- The plugin publishes `STATE.workflow_registry` + `STATE.workflow_run`, so the core chat
  `/<recipe>` slash-command keeps working **by inversion** — it reads those (both `None`
  when the plugin is off) instead of importing workflows.
- Core is fully stripped: no `run_manual_workflow`, no `_build_task_tools` workflow branch,
  no `_build_workflow_registry`, no `/api/workflows`, no `workflows_enabled` config;
  `graph/workflows/` and the bundled recipes are deleted (recipes now ship in the plugin).

**3. The Studio surface stays native React, via the `src/ext` seam (NOT an iframe).**

`src/ext/workflows.tsx` calls `registerSurface(...)`; `ExtSurface` gains `requiresPlugin`,
so the rail item is hidden and the surface unreachable unless the plugin is enabled. No
iframe rewrite — `src/ext` (ADR 0038 D3) already hosts trusted in-process React. The
surface JS is bundled but inert when off ("not active by default", not "zero JS").

## Consequences

- **Lean core**: the workflow engine/tools/recipes/API don't load unless the plugin is
  enabled. Default build is smaller; workflows is opt-in.
- **The SDK is the durable artifact.** It establishes the contribution-vs-consumption
  contract; future "plugins tapping core" reuse `graph/sdk.py` rather than reaching in.
- **Studio = Workflows** (ADR 0020) is now a plugin-contributed, gated surface — the
  "control stack" (ADR 0009) altitude for workflows lives in the plugin.
- **Tradeoff**: the chat slash-command + the bundled-but-gated surface keep a thin core
  touch-point (`STATE.workflow_run`, `requiresPlugin`). Truly-zero-core would mean a
  plugin-contributed slash-command hook + an iframe surface — deferred; the inversion is
  clean enough and preserves the UX.
- **Cross-plugin recipes (ADR 0027)** still work for plugins loaded before workflows; a
  recipe dir from a later-loading plugin is a known edge (rare) — revisit with lazy
  registry build if it bites.
