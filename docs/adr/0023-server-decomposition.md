# ADR 0023 — Decompose server.py: an AppState container + a composition root

- **Status:** Done (2026-06-05) — fully shipped across all three phases, each a live-smoked PR. Phase 1 #547; phase 2 #549–#552; phase 3 #554–#558. `server.py`'s 3,353 lines are now a ~700-line `server/` package composition root.
- **Date:** 2026-06-05
- **Deciders:** Josh Mabry; protoAgent maintainers
- **Tags:** architecture, refactor, server, maintainability
- **Supersedes / Superseded by:** —

> `server.py` is **3,353 lines — 3.2× the next-largest file** — and it isn't one
> concern: it's the CLI entry, agent init/reload, the store/scheduler/skills/
> workflow builders, the chat backend, the A2A wiring, *and* ~26 HTTP route
> handlers, with `_main()` alone at **1,084 lines**. The root cause is **26
> ambient module-global singletons**: every helper that touches runtime state
> reaches for a global, so it must live in (or close over) `server.py`. This ADR
> replaces the ambient globals with an **AppState** container and splits the file
> into a thin composition root + focused modules — **zero functional change**.

---

## 1. Context & Problem statement

`server.py` works (the suite is green), so this is a maintainability cost, not a
bug — but a real one for a **template others fork and edit**: it's where merge
conflicts cluster (it changed in roughly half the PRs of the session that
prompted this ADR), where new contributors get lost, and where the
unit-test-vs-wire-test gap lives.

Two structural causes:

1. **26 ambient module-global singletons** — `_graph`, `_graph_config`,
   `_knowledge_store`, `_skills_index`, `_workflow_registry`, `_telemetry_store`,
   `_inbox_store`, `_activity_log`, `_storm_guard`, `_checkpointer`, the MCP /
   plugin lists, … Functions read them directly and write them via `global`. So
   nothing that touches state can move out of `server.py`. This is the disease;
   the 1,084-line `_main()` is the symptom.
2. **The `operator_api/` extraction is half-done.** `register_operator_routes`
   already exists and takes ~22 handler callbacks — but the callback *bodies* are
   still defined inline in `_main()` as `_operator_*` closures over the globals.
   Only the URL-wiring moved out; the logic stayed.

## 2. Decision

**Introduce an `AppState` container, then extract by concern; `server.py`
becomes a composition root (~300–400 lines).**

### 2.1 `runtime/state.py` — AppState

A single dataclass holds the 26 fields (graph, config, the stores, registries,
MCP/plugin state, scheduler, background-task handles). A module-level singleton
`STATE` replaces the ambient globals; `get_state()` returns it (usable as a
FastAPI dependency). Functions read `state.knowledge_store` instead of the
global `_knowledge_store`; init/reload mutate `state.*` instead of `global _x`.
The migration is mechanical and behavior-preserving — same objects, same
lifecycle, named field access instead of ambient names.

### 2.2 Extract by concern

With state no longer ambient, the big regions move to their own modules, each
importing `STATE`:

- `server/agent_init.py` — `_build_*` (knowledge/scheduler/skills/workflow),
  `_init_langgraph_agent`, `_reload_langgraph_agent`, settings callbacks.
- `server/chat.py` — `_chat_langgraph_stream`, `_run_turn_stream`, the slash-
  command parsing (workflow + subagent).
- `server/a2a.py` — the a2a-sdk wiring, `ProtoAgentExecutor` hookup, the terminal
  hook, `_record_a2a_telemetry`.
- `operator_api/` route modules — finish the extraction: move the inline
  `_operator_*` / `_api_*` route bodies into `knowledge.py`, `activity.py`,
  `telemetry.py`, `config_routes.py`, `chat_routes.py`.

`server.py` keeps only: arg parsing, the FastAPI app, and the wiring that
composes the modules — the composition root.

### 2.3 Safety protocol (load-bearing)

This is a refactor of the app core with **zero intended functional change**, so
the risk is regression, not design. Therefore:

- **One phase per PR**, in dependency order: AppState → backends (chat/a2a/init)
  → route groups. Each is independently reviewable and revertable.
- **Every PR is green on the full suite AND live-smoked** against a throwaway
  instance (boot, a chat turn, the A2A round-trip, the knowledge store, an inbox
  fire) before merge — per the smoke-test lesson that caught three wire-level
  bugs this same session.
- No behavior changes ride along — pure moves + the state rename.

## 3. Consequences

- `server.py` becomes navigable; route/backend logic lives with its concern.
- State is explicit and injectable — testable without monkeypatching module
  globals, and a step toward catching the wire-level gaps in CI.
- Finishes the `operator_api/` extraction the codebase already started.
- Short-term churn + regression risk, mitigated by the phasing + live smoke.

## 4. Implementation (phased)

1. **AppState** ✅ *shipped (#547)* — `runtime/state.py` + replace the 26
   globals. Biggest, most mechanical; the foundation everything else stands on.
2. **Backends** ✅ *shipped (#549–#552)* — extracted as a `server/` package
   (`server.py` could not coexist with a `server/` dir, so it was promoted to
   `server/__init__.py` + `server/__main__.py`; launch is now `python -m
   server`). Then one PR each: `server/a2a.py` (#550), `server/chat.py` (#551),
   `server/agent_init.py` (#552). The composition root dropped **3,353 → ~1,354
   lines**; each PR was live-smoked (boot, chat turn, A2A round-trip, hot
   reload). The cross-module shared surface stayed tiny — `STATE` plus
   `agent_name` / `AGENT_NAME_ENV` / `_event_bus` / `_bundle_root`, re-exported
   from `__init__` so `server.<symbol>` is unchanged.
3. **Route groups** ✅ *shipped (#554–#558)* — moved the inline `_main()` route
   bodies into `operator_api/*` registrars: `telemetry_routes.py`,
   `knowledge_routes.py`, `config_routes.py`, `chat_routes.py` (each a
   `register_*_routes(app)`), plus the 21 React-console handler closures into
   `console_handlers.py` (finishing the half-done `operator_api/` extraction).
   `_main()` is now ~430 lines of pure app assembly; the whole `server/__init__.py`
   is ~700. Each route group ships its own unit tests (testable on a bare
   `FastAPI()` app, no boot) and was live-smoked end-to-end.

**Outcome:** the ~3.35k-line monolith is a ~700-line composition-root package +
focused modules (`server/{a2a,chat,agent_init}.py`, `operator_api/*_routes.py`).
The fork-and-edit core is navigable; state is explicit (`STATE`) and injectable.

## 5. Alternatives considered

- **Finish route extraction only** (leave the global model). Rejected as the
  end state: the extracted routes would still import the ambient globals, so the
  coupling just spreads across files. (It's a valid *first* increment, but
  AppState is the actual fix.)
- **Split by concern without AppState.** Same problem — every moved function
  drags the globals with it.
- **Leave it.** Rejected: it's the fork-and-edit core; navigability compounds.
