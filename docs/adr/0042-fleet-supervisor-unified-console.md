# 0042 — Fleet supervisor & unified console (background agents, in-place switching)

- Status: Accepted (v0.31.0). Member launch now sets `PROTOAGENT_HOME=<workspace>` (not the retired
  `PROTOAGENT_CONFIG_DIR`) per [ADR 0065](0065-two-tier-instance-paths.md); the fleet model is
  otherwise unchanged.
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

> **Amendment (v0.35.0, #883/#900):** the slug proxy also forwards **WebSocket upgrades**,
> not just HTTP/SSE. The original proxy stripped `Upgrade`/`Connection`, so a member's
> plugin view that opened a live WS (e.g. `agent_browser`'s viewport) loaded over HTTP but
> its socket showed "Disconnected" behind the hub. `proxy.forward_ws` resolves the slug →
> member, opens a client WS (carrying the bearer + subprotocols), and pumps frames both
> ways — so WS plugin views traverse the hub like HTTP does.

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

### H. Native desktop — the Tauri shell *is* the hub

On the desktop app (`apps/desktop`, Tauri), the **shell plays the hub**. It already spawns +
supervises the server as a sidecar (the parent-death watchdog / autostart machinery), so it
takes the supervisor + proxy roles directly and the fleet becomes **GUI-first** — the CLI's
`workspace`/`fleet`/`plugin` verbs become panels over the *same* control-plane API, so native
and headless never diverge.

**Roles on native**
- **Process management** — the shell spawns each agent as a sidecar (`python -m server
  --ui none` per workspace, via `workspaces.run_exec` / the `fleet` API), tracks pid+port,
  reaps them on quit (reusing its server-sidecar machinery), and applies the keep-N-warm
  policy (a laptop won't run a big fleet hot).
- **Proxy** — the in-app webview renders one console; the shell (or a thin in-process hub it
  sidecars) routes the active console's chat / A2A / SSE to the selected agent. Loopback + a
  shell-held token — no per-agent surface is exposed.
- **Contract** — the panels drive the same `/api/fleet` + `/api/archetypes` the CLI uses.

**Panels (React, `apps/web`)**
- **Onboarding** — first run becomes *"create your first agent"*: an **archetype picker**
  (cards from `GET /api/archetypes` — **Basic** + every installed bundle's `archetype:`) →
  name → `POST /api/fleet` (create + start). Replaces the single-agent setup wizard.
- **Switcher** — the topbar agent name → a dropdown (fleet list + status dots + "+ New agent").
- **Fleet manager** (Settings → Agents) — `GET /api/fleet` rows with start/stop/remove + "+ New."
- **Per-agent config** — the *existing* Settings drawer (model / plugins / secrets), now
  scoped to the **active** agent's workspace (config is per-`PROTOAGENT_CONFIG_DIR`).

**Build seam (who builds what)**
- *Server*: the `fleet` supervisor (slice 1, shipped) + the control-plane API & reverse proxy
  (slice 2) + `run_exec` (the launch primitive).
- *Tauri shell*: the agent-sidecar spawn/reap + proxy plumbing (recommended — it's already
  supervising a sidecar), **or** delegate both to a thin Python hub it sidecars. Either works.
- *React*: the onboarding/archetype picker, switcher, and fleet-manager panels — all over the
  control-plane API.

Net: the desktop just **renders the control plane the CLI already drives** — one model, two
front-ends.

### I. Remote fleet members — agents on other machines

The slices above manage **local** agents: the supervisor `run_exec`s a subprocess on *this*
host, tracks it by pid, and the proxy forwards to `127.0.0.1:<port>`. But the model already
generalizes to **remote** protoAgents on other machines — the only thing that's intrinsically
local is *process spawning*. Everything else is address-based:

- **Each agent is already an independent A2A endpoint** (the `a2a` field is a URL). Nothing
  makes it loopback-only — point it at `https://host:port` and agent↔agent `delegate_to`
  works cross-machine today.
- **The reverse proxy is address-based** — `/active/*` forwards to "the active agent's
  address." Localhost now; a remote `host:port` needs no console change.
- **The host already self-registers** as a first-class agent (`host: true`) — a *remote*
  member is the same idea with `remote: true` and a non-local address.

**A remote member is *registered*, not spawned.** Add it by URL + token (not create+launch):

- **Agent kind** — `supervisor.status()` gains a `remote: true` member kind: `{name, kind:
  "remote", url, running, a2a: <url>/a2a}`. No local pid.
- **Status** — health is a poll of the remote's `GET /api/runtime/status` (+ the hub token),
  not the local `_alive(pid)` probe. Unreachable ⇒ `running: false` (same UX as a crash).
- **Lifecycle** — you can't `kill` a remote process. Two tiers: (a) **register-only** — the
  remote runs itself; the hub bookmarks + proxies + monitors it, no start/stop; (b) **remote
  hub** — if the other machine runs its *own* fleet hub, start/stop proxies to *its*
  control-plane API (turtles all the way down).
- **Transport/auth** — real network now: TLS + a per-remote token (reuse the existing A2A /
  console bearer-gate). The proxy attaches the token on forward.

**Relationship to the delegate registry (ADR 0025).** protoAgent *already* connects to remote
agents — `delegate_to` over a2a/openai/acp (Settings → Integrations). That's the **delegation**
axis (hand a remote a task). The fleet is the **management** axis (switch your console *into* an
agent, see its status, control its lifecycle). Remote fleet members are the **convergence**: a
registered remote could surface in *both* — delegate to it as a peer **and** focus the console
on it. A future cleanup is one registry feeding both views.

**Frontend is mostly free** — the panels render `/api/fleet`; a remote agent is just an agent
with a remote address + `remote: true`. Show a "remote" tag (like the `host` tag), gate
start/stop on the lifecycle tier, and the switcher's address swap already routes through the
proxy. The only real new UI is the **"add remote agent"** form (URL + token) alongside "+ New."

Honest scope: this is **not built** — it's the designed-for next axis. The proxy +
independent-endpoint + self-registration design were chosen so it's an extension, not a
rewrite.

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
6. **Remote fleet members** (§I, **shipped — #839, register-only tier**) — a `remote: true`
   member registered by URL (+ optional bearer, stored hub-side and attached by the proxy):
   `remotes.json` beside `fleet.json`, status via a TTL-cached agent-card probe (refreshed off
   the event loop), `_target_for_slug` resolves remote slugs to URLs, Discover's "Add to this
   fleet" + `POST/DELETE /api/fleet/remotes`. Composes with the delegate registry (ADR 0025) —
   the same remote can be a member *and* a `delegate_to` target. Remote-hub start/stop (tier b)
   remains future work.

Slices 1–3 deliver the core (switch between running agents in one console); 4 adds the
starter-type creation flow; 5 makes it scale; 6 extends the fleet across machines.
