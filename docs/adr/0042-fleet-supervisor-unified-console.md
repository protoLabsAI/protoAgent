# 0042 — Fleet supervisor & unified console (background agents, in-place switching)

- Status: Proposed
- Date: 2026-06-09
- Builds on: ADR 0004 (per-instance scoping), 0027 (plugins), 0040 (bundles), 0041
  (workspaces & tiered stores).

## Context

ADR 0041 made each agent a first-class, isolated **workspace** — its own config dir,
its own `instance.id`-scoped data, its own port. But a workspace is still a *separately
launched server with its own console*. The operator wants a **fleet**: many named agents
on one machine, switchable from **one console**, each **running in the background** with
its **session intact** — flip from Ava to Roxy and back, and Ava's chat (and her in-flight
background work) is right where you left it.

Crucially, the hard half is already done. Each agent's chat history is in its own
`~/.protoagent/<id>/checkpoints.db` (ADR 0004/0041), so "Ava's session continues" is just
reconnecting to Ava's thread — and it survives a restart (resume from checkpoint). What's
missing is (a) something that **keeps the processes alive** so *background activity*
(schedules, an in-flight agent loop) continues while you're away, (b) a console that
**switches which agent it's viewing in place**, and (c) a way to **spawn new agents from a
starter type**.

## Decision

A **fleet supervisor + a unified, proxying console**, over persistent background agents,
with **archetype**-driven creation. Recommended topology:

```
        ┌──────────── Hub (:7870) — the only UI ────────────┐
        │  Unified console + Supervisor + Reverse proxy      │
        │  [ Ava ▾ ]  switch = swap the proxy's active target│
        └───┬─────────────────┬─────────────────┬────────────┘
   supervises headless agent backends (--ui none), each isolated:
     ┌──────▼───┐      ┌───────▼───┐      ┌──────▼────┐
     │ ava :7901│      │ roxy :7902│      │trader:7903│   ← persistent bg processes
     │ scoped   │      │ scoped    │      │ scoped    │      (own data + live session)
     └──────────┘      └───────────┘      └───────────┘
```

- **Hub** — a lightweight server on the main port that serves the **one console**, runs
  the **supervisor**, and **reverse-proxies** the active console's API / A2A / SSE to the
  selected agent backend. The operator only ever opens the hub.
- **Agents** — ordinary workspace servers launched **headless** (`--ui none`, API+A2A
  only — lighter, no console each), supervised by the hub, each scoped per ADR 0041.
- **Switching is view-only** — selecting an agent swaps the proxy target; the agent never
  stops, so its background work keeps running and its session is live.
- **Start/stop from the control plane** — the hub can launch, stop, and report status for
  each agent; a stopped agent's history persists and resumes when restarted.
- **Archetypes** — a bundle (ADR 0040) carries an `archetype:` block (label/icon/blurb);
  the new-agent picker offers each known bundle as a starter type, and "create" runs the
  ADR 0041 `workspace new --bundle` flow.

## Design details

### A. The supervisor (control plane)
Owns agent-process lifecycle, built on `workspaces.manager.run_exec` (ADR 0041): for each
workspace it can **start** (spawn `python -m server --ui none` with the workspace's
config-dir + instance + port), **stop** (SIGTERM → reap), and report **status** (running /
stopped, pid, port, uptime). A small registry (in `workspace.yaml` + live process table)
tracks it. Exposed as a CLI (`python -m server fleet up/down/ls/status`) and the API below.

### B. Control-plane API (served by the hub)
- `GET /api/fleet` — every workspace + live status (running/stopped, port, the active one).
- `POST /api/fleet/{name}/start` · `POST /api/fleet/{name}/stop`.
- `POST /api/fleet/{name}/activate` — set the console's proxy target.
- `POST /api/fleet` — create from an archetype (`{name, bundle}`) → `workspace new --bundle`.
- `GET /api/archetypes` — known bundles' `archetype:` metadata for the new-agent picker.

### C. The reverse proxy
The hub forwards the active console's requests (`/api/chat`, `/a2a`, `/api/events` SSE,
plugin views) to the active agent's loopback backend, streaming SSE through unbuffered.
**Auth**: agents bind loopback and trust a shared hub token (the hub injects it); the
browser only ever talks to the hub (same-origin), so no per-agent CORS/token sprawl.

### D. Session continuity (≈ free)
Each agent's checkpoints/goals/memory are already `instance.id`-scoped (ADR 0004/0041) —
so switching back loads that agent's thread, and it survives stop→restart (resume from
checkpoint). The supervisor keeping the process **warm** is what additionally keeps
*background activity* (scheduler ticks, an in-flight agent loop) running while you're away.

### E. The switcher UI
The console topbar agent name becomes a dropdown: the fleet list with status dots + ports,
a one-click switch (swap active), start/stop affordances, and **"+ New agent"** → the
archetype picker (cards from `GET /api/archetypes`) → name → create + start + activate.

### F. Archetypes (additive — bundle metadata)
A bundle manifest gains an optional `archetype:` block (`label`, `icon`, `blurb`, optional
persona/accent). It rides the **settled bundle shape** — `load_bundle` already returns it,
no schema change. pm-stack is archetype #1 ("Project Manager"); every future bundle that
adds the block becomes a starter type for free.

### G. Keep-alive policy
Resource-bound: default **keep-N-warm** (recently-active agents stay running; the rest are
stopped and resume from checkpoint on switch), with an opt-in **run-all** for small fleets.
Configurable in the hub.

## Options considered

- **Separate consoles per agent** (ADR 0041 slice-4 simple) — navigate to each agent's own
  console. Ships fast, but no single home, no in-place feel, no shared background view.
  Rejected as the end state.
- **In-place hot-swap via a hub proxy** (this). Slickest UX (one console, swaps in place),
  supports background agents + start/stop + a single auth boundary. Chosen.
- **Eager run-all vs keep-N-warm.** Run-all is simplest but doesn't scale; keep-N-warm
  bounds resources while history always persists. Chosen (configurable).
- **Archetype source: bundle metadata vs a curated registry.** Bundle metadata is
  self-describing and needs no second list to maintain. Chosen.

## Consequences

- **A real fleet OS** — one console over many persistent, isolated, archetype-spawned
  agents, with lifecycle control. The capstone of 0040/0041.
- **Resource cost** — N warm agents ≈ N processes + model connections; the keep-N-warm
  policy bounds it, and history persists regardless of warmth.
- **The hub is now critical** — it owns the console, the proxy, and the supervisor; it must
  fail safe (an agent crash shows as "stopped", never takes the hub down).
- **Security** — agents bind loopback + trust a hub token; the browser only touches the
  hub. No new external surface.
- **Backward compatible** — a single `python -m server` (no hub) is unchanged; the fleet is
  opt-in via the hub.

## Implementation slices

1. **Supervisor** — start/stop/status over `run_exec`, a process table, `fleet` CLI.
2. **Control-plane API + reverse proxy** — `/api/fleet` + active-target proxying (chat/A2A/SSE).
3. **Switcher UI** — the agent-name dropdown (list, status, switch, start/stop) over the API.
4. **Archetypes** — `GET /api/archetypes` + the "+ New agent" picker → create-from-archetype.
5. **Keep-alive policy** — keep-N-warm + resume, lifecycle polish.

Slices 1–3 deliver the core (switch between running agents in one console); 4 adds the
starter-type creation flow; 5 makes it scale.
