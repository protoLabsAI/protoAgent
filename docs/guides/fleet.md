# Fleet — many agents on one host

Run several named agents on one machine, each fully **isolated**, each runnable in the
**background**, each built from a reusable **archetype** — and switchable in place from
**one console** (slug-routed, per-agent layout/theme). The fleet is a handful of composable
primitives:

| Primitive | What it is | ADR |
|---|---|---|
| **Workspace** | a named agent — its own config, secrets, plugins, scoped data, port | [0041](../adr/0041-workspaces-and-tiered-stores.md) |
| **Bundle** | a curated, pinned set of plugins installed as one | [0040](../adr/0040-plugin-bundles.md) |
| **Archetype** | a bundle presented as a starter *agent type* (or the built-in **Basic**) | [0042](../adr/0042-fleet-supervisor-unified-console.md) |
| **Tiered stores** | per-agent private data + an opt-in shared **commons** | [0041](../adr/0041-workspaces-and-tiered-stores.md) |
| **Supervisor** | run agents as persistent background processes (start/stop/status) | [0042](../adr/0042-fleet-supervisor-unified-console.md) |
| **Unified console** | one slug-routed console that hot-swaps between running agents (per-agent layout/theme) | [0042](../adr/0042-fleet-supervisor-unified-console.md) |

## Quick start

```bash
# an agent from the "Project Manager" archetype (the pm-stack bundle)
python -m server workspace new pm --bundle https://github.com/protoLabsAI/pm-stack

# a blank-slate agent (the built-in Basic archetype — core loop + tools, no plugins)
python -m server workspace new scratch

# run the whole fleet in the background, then look at it
python -m server fleet up
python -m server fleet ls
#   ● pm        :7871  pid 12345  [pm-stack]
#   ● scratch   :7872  pid 12346
```

## Workspaces — a named, isolated agent

A **workspace** is a directory that *is* an agent. Its `langgraph-config.yaml`,
`secrets.yaml`, `plugins.lock`, and `config/plugins/` live there (so
`PROTOAGENT_CONFIG_DIR=<ws>` is its whole identity), and `instance.id = <name>` scopes its
**private data** (goals, chat history, memory, knowledge) to `~/.protoagent/<name>/*` — so
agents on one host never collide (the leak that motivated this; see
[multi-instance](./multi-instance.md)).

```bash
workspace new <name> [--from <cfg>] [--bundle <url>] [--port auto] [--shared-skills]
workspace ls
workspace run <name>          # foreground: execs the normal server, env wired in
workspace rm <name> [--purge] # --purge also deletes its scoped data
```

`--from <dir>` clones an existing agent's config + secrets (re-stamping identity/instance);
`--bundle <url>` installs a bundle into it (next section); `--port auto` picks a free port.

## Bundles & archetypes — start from a type

A **bundle** ([ADR 0040](../adr/0040-plugin-bundles.md)) is a repo whose
`protoagent.bundle.yaml` names a *pinned set of plugins* to install together, plus a
suggested enable list + config. Install one into a workspace and you skip the
plugin-by-plugin setup:

```bash
python -m server plugin install https://github.com/protoLabsAI/pm-stack   # fans out + pins each member
```

A bundle that carries an **`archetype:`** block becomes a **starter agent type** the
new-agent picker offers — additive metadata, no change to the bundle shape:

```yaml
# protoagent.bundle.yaml
id: pm-stack
plugins: [ … ]
enabled: [ … ]
archetype:
  label: Project Manager
  icon: LayoutGrid
  blurb: Board-driven shipping agent — decomposes an idea and ships it via coding agents.
```

Two starter types exist today:

- **Basic** — built-in, ships with protoAgent: the bare agent loop + built-in tools, **no
  plugins**. It's just `workspace new <name>` with no `--bundle` (the "start from scratch").
- **Project Manager** — the [pm-stack](https://github.com/protoLabsAI/pm-stack) bundle.

Every future bundle that adds an `archetype:` block becomes a starter type for free. See
[Install & publish plugins](./plugin-registry.md).

## Tiered stores — private by default, share what should be shared

Each agent's stores are **scoped** (private) by default. **Skills** can be tiered so a fleet
shares a growing skill library while keeping the rest private —
[ADR 0041](../adr/0041-workspaces-and-tiered-stores.md):

```yaml
skills:
  scope: scoped | shared | layered   # default: scoped
commons:
  path: ""    # shared-tier base dir; blank → ~/.protoagent/commons
```

- **scoped** — a private skills DB per agent.
- **shared** — one commons DB the whole fleet reads *and* writes.
- **layered** — *shared brain, private hands*: read the commons ∪ your private library, but
  **writes go to private**, so half-baked learned skills never pollute the fleet. Lift a
  proven one up explicitly:

```bash
python -m server skills ls               # private + commons, tagged by tier
python -m server skills promote <name>   # a private skill → the shared commons
```

## The supervisor — agents in the background

Run the fleet as persistent background processes — [ADR 0042](../adr/0042-fleet-supervisor-unified-console.md):

```bash
python -m server fleet up [names…]    # start agents — all workspaces, or named
python -m server fleet ls             # ● running / ○ stopped + port + pid
python -m server fleet down [names…]  # stop agents
```

Each agent is an ordinary headless server (`--ui none`) on its workspace's port, tracked in
a `fleet.json` registry. Because each agent's chat history is scoped to its own
checkpoints, a **stopped agent's session resumes** when you restart it — and a **running**
one keeps its background work (schedules, an in-flight loop) going while you're elsewhere.

## The unified console — every agent in one UI

*(Shipped — ADR 0042 slices 2–5.)* The **hub** (any running agent) serves one console and
reverse-proxies each agent window's chat / A2A / SSE / WebSockets to that agent's backend,
keyed by the **URL slug** (`/app/agent/<id>/`) — so every window targets its own agent:
switch in place from the topbar, or open two agents in two windows at once. Per-agent chat,
theme and layout follow the slug; a stopped agent **resumes from its checkpoint** when you
navigate to it; "+ New agent" runs the archetype picker. A plugin view served by a member
that opens a **WebSocket** (e.g. `agent_browser`'s live viewport) works through the hub too:
the slug proxy forwards WS upgrades, not just HTTP/SSE ([#883](https://github.com/protoLabsAI/protoAgent/issues/883), shipped v0.35.0). Settings → Agents is the fleet manager
(create / start / stop / rename / remove), and **Discover** finds other protoAgents on the
box, the LAN (mDNS) and your **tailnet** (via the Tailscale CLI).

## Remote fleet members — the agent there, the UI here

*(ADR 0042 §I.)* A fleet member doesn't have to be local: register any reachable protoAgent
by URL and it becomes a **switchable member** — a slug window like any peer, with the hub
reverse-proxying its console + A2A. The remote runs fully headless; this console is its UI.

On the other machine:

```bash
A2A_AUTH_TOKEN=<secret> python -m server --port 7871 --host 0.0.0.0 --ui none
```

On this one — Settings → Agents → **Discover** → **➕ Add to this fleet**, or register
manually (the stored token is attached by the proxy; the browser never sees it):

```bash
curl -X POST http://127.0.0.1:7871/api/fleet/remotes \
  -H 'content-type: application/json' \
  -d '{"name": "ava", "url": "http://100.101.189.45:7871", "token": "<secret>"}'
```

Remote members show a `remote` tag + their URL in the fleet manager; `running` is a cached
reachability probe. You can't start/stop/rename them from here — their deployment owns
their lifecycle; **Remove** only unregisters (the remote agent is untouched). Registering
as a member and adding as a [`delegate_to` target](delegates.md) compose: the same agent
can be both a window you operate and a delegate your agents call.

**Version skew is flagged.** The hub console drives a remote's full `/api/*` by proxy, so a
remote on a *different protoAgent release* is a real compat surface. The reachability probe
also reads the remote's app version off its A2A agent card; when it differs from the hub's,
the fleet manager shows a warning badge on that member ("remote runs vX.Y.Z, the hub
vA.B.C — features may misbehave"). Upgrade the lagging side to clear it.

## See also

- ADRs: [0040 bundles](../adr/0040-plugin-bundles.md) ·
  [0041 workspaces & tiered stores](../adr/0041-workspaces-and-tiered-stores.md) ·
  [0042 fleet supervisor & unified console](../adr/0042-fleet-supervisor-unified-console.md)
- Guides: [multi-instance scoping](./multi-instance.md) · [plugins](./plugins.md) ·
  [install & publish plugins](./plugin-registry.md) · [skills](./skills.md)
