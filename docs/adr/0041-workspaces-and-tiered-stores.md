# 0041 — Workspaces & tiered stores (the fleet-on-one-host model)

- Status: Proposed
- Date: 2026-06-09
- Builds on: ADR 0004 (per-instance data scoping), ADR 0027 (git-installable plugins), ADR 0040
  (plugin bundles).
- Motivated by: a real incident — roxy ran unscoped on a host shared with a `protoTrader`
  agent, and protoTrader's goals + knowledge leaked into roxy through the shared
  `~/.protoagent/*` root. The fix (set `instance.id`) revealed that the primitives for
  full multi-agent isolation already exist; they're just not packaged as a first-class concept.

## Context

Running several agents on one machine is a first-class need (an operator fleet, a dev box, a
single deploy host). protoAgent already has every isolation primitive:

- **`PROTOAGENT_CONFIG_DIR`** redirects `_live_config_dir()` — and `langgraph-config.yaml`,
  `secrets.yaml`, `plugins.lock`, and `config/plugins/` all resolve under it. **One config dir =
  one agent's entire identity + capabilities.**
- **`instance.id`** (→ `PROTOAGENT_INSTANCE` via `_seed_instance_env`, → `scope_leaf`) namespaces
  the per-agent data stores to `~/.protoagent/<id>/*` (ADR 0004).
- **`--port`** gives each running agent its own bind.

So an agent is *already* a `(config-dir, instance-id, port)` triple. But:

1. There is **no first-class "agent on this box" object** — you hand-juggle three env knobs, which
   is exactly how the leak happened (nobody set `instance.id`).
2. Isolation is **all-or-nothing**. `scope_leaf` either scopes *every* store to one id, or shares
   *every* store at the root. There is no way to say "share the skills library, keep goals private"
   — which is what a fleet actually wants. The shared root is a footgun *because* it's
   undifferentiated, not because sharing is wrong.

This ADR turns both into features: **workspaces** (a named, self-contained agent) and **tiered
stores** (a deliberate shared commons + per-agent private layer).

## Decision

Introduce two composable concepts:

### A. Workspaces — a named, self-contained agent on the host

A **workspace** is a directory that *is* an agent:

```
workspaces/
  roxy/
    langgraph-config.yaml   secrets.yaml         # who it is (model, plugins, persona, delegates)
    plugins.lock            config/plugins/      # its capabilities (ADR 0027/0040)
    data/                   # its PRIVATE stores (goals, checkpoints, working memory, …)
    workspace.yaml          # { id: roxy, port: 7871, commons: <path>, created, bundle?: pm-stack }
  protoTrader/
    …
```

Running a workspace = set `PROTOAGENT_CONFIG_DIR=<ws>`, `PROTOAGENT_INSTANCE=<id>`, `--port <port>`,
and point the private store paths at `<ws>/data/`. Because everything lives under one dir, a
workspace is **portable** (zip it, copy it to another box) and **disposable** (`rm -rf`).

A thin **`workspace` CLI** manages them (no new runtime — it orchestrates the existing knobs):

```
protoagent workspace new <name> [--bundle <url>] [--from <template>] [--port auto]
protoagent workspace ls                       # registry: name, id, port, status, bundle
protoagent workspace run <name>               # sets CONFIG_DIR + INSTANCE + port, starts the server
protoagent workspace rm <name> [--purge]      # remove the dir (and scoped commons refs)
protoagent workspace switch <name>            # for sequential single-agent use (the "current" ws)
```

### B. Tiered stores — a shared commons + a per-agent private layer

Each store is classified **shared** (the commons, read by every workspace) or **scoped** (private
to the workspace). Default split:

| Store | Tier | Why |
|---|---|---|
| **skills** (`SkillsIndex`, learned + disk SKILL.md) | **shared** | a skill one agent learns, all can reuse — the canonical commons |
| **knowledge — curated/reference** | **shared (opt-in)** | org/fleet facts an operator promotes; read by all |
| **knowledge — working** | scoped | an agent's in-progress facts are its own |
| **goals** | scoped | an agent's objectives are its own |
| **checkpoints** (chat history) | scoped | per-thread, per-agent |
| **memory** (session summaries) | scoped | per-agent conversational memory |
| **activity** | scoped | per-agent event log |

**Read model:** a shared-tier store reads the **commons** (a designated shared location, default
`~/.protoagent/commons/` or a configurable path); a scoped store reads `<ws>/data/` (or
`~/.protoagent/<id>/`). 

**Read-through + promotion (richer mode):** a store may run **layered** — read = `commons ∪ private`,
write = `private`, with an explicit **`promote`** to lift a proven private skill/fact into the
commons. This is the full "shared brain, private hands" model; whole-store-shared (below) is the
simple first cut.

The leak becomes a feature: *share what should be shared, keep the rest private — by design.*

### Two layers — config (per-workspace) vs data (tiered)

The model has two distinct layers, and it's important not to conflate them:

- **Config layer — always fully per-workspace** (under `PROTOAGENT_CONFIG_DIR = <ws>`): the
  `langgraph-config.yaml`, `secrets.yaml`, the **installed plugins** (`config/plugins/`), the
  **`plugins.enabled` list + each plugin's config section**, `plugins.lock`, and the setup marker.
  Every workspace has its **own plugin set, its own bundle, its own secrets, its own model + persona**
  — they never share these. So *yes, plugins (and all of that) are scoped per workspace by
  construction.* A board agent enables `project_board`; a trader enables a finance plugin; neither
  sees the other's tools, config, or secrets.
- **Data layer — tiered** (the stores above): private per workspace (goals/checkpoints/memory/
  working-knowledge), with an opt-in shared commons (skills + curated knowledge).

Sharing at the config layer is done by **reference, not by a shared mutable store**: a **bundle**
(ADR 0040) is how two workspaces install the *same* capability stack, and each still gets its own
isolated copy + config. (A shared on-disk **plugin cache** — dedupe identical pinned plugins across
workspaces instead of N clones — is a possible later optimization, but it changes nothing about
isolation: enable/config/secrets stay per-workspace.)

## Design details

1. **Per-store tier flag.** Each store's config gains `scope: shared | scoped` (default per the
   table). Implementation: the path resolver applies `scope_leaf` only for `scoped` stores; a
   `shared` store resolves to the commons path (un-scoped). This is the **first slice** — small,
   touches only the path-resolution seams already centralized around `scope_leaf`
   (`graph/config_io.py`, `graph/goals/store.py`, `graph/middleware/memory.py`, `server/agent_init.py`).
2. **The commons location** is configurable (`commons.path`, default `~/.protoagent/commons/`); a
   workspace records the commons it points at in `workspace.yaml`, so a fleet shares one commons
   while each keeps its own private dir.
3. **Read-through layering (slice 3)** is per-store opt-in (`scope: layered`): the store opens both
   the commons and the private backend, unions reads, writes private. Skills and knowledge (FTS5 +
   vector) support a base+overlay read; goals/checkpoints stay strictly scoped (no layering).
4. **Promotion (slice 3)**: `protoagent skill promote <id>` / a `knowledge_promote` tool moves a
   private artifact into the commons (curated, never automatic — the commons is trusted).
5. **Port allocation**: `--port auto` picks a free port; the workspace registry records it so
   `ls` shows what's where and `run` reuses the assigned port.
6. **Bundle composition (ADR 0040)**: `workspace new <name> --bundle <url>` scaffolds the workspace
   **and** installs the bundle into its `config/plugins/` + pins it in the workspace's
   `plugins.lock`. Bundles = the capability stack; workspaces = the isolated instance.

## Options considered

- **Keep the env-knob status quo.** Works, but it's the footgun that caused the leak and has no
  shared-commons story. Rejected.
- **One shared root, no scoping** (the accidental pre-fix state). Simple, but agents collide.
  Rejected — it's the bug.
- **Full isolation, no commons** (just ADR 0004 per-store scoping everywhere). Safe, but loses the
  fleet's biggest win: a shared, growing skills/knowledge library. Rejected as the *end* state;
  kept as the default for the scoped tier.
- **Tiered stores + workspaces** (this ADR). The commons is deliberate and curated; the private
  layer is per-workspace; the CLI makes each agent a first-class object. Chosen.
- **Data in `~/.protoagent/<id>/` vs inside the workspace dir.** Both work (path env vars).
  Recommended: **private data inside the workspace dir** (self-contained, portable), **commons in a
  shared location**. A workspace stays one movable folder; the commons is the one thing it reaches
  outside itself.

## Consequences

- **Fleet-on-one-host becomes first-class and safe** — `workspace new/run` instead of three env
  knobs; no more accidental cross-agent leaks.
- **A shared brain** — skills (and curated knowledge) accumulate once and benefit every agent; the
  commons is the fleet's compounding asset.
- **Portable, disposable agents** — a workspace is a folder you can copy, archive, or delete.
- **Composes cleanly** — workspaces (isolation) × bundles (capabilities) × tiered stores (shared
  vs private) is one coherent model with no overlap.
- **Migration**: existing single-agent installs are unaffected (default `scope: scoped` for the
  private set + no commons = today's behavior; opt into shared skills + a commons explicitly). The
  legacy shared root (`~/.protoagent/{skills.db,knowledge}`) can be *adopted* as the commons.
- **Risk — commons trust**: a shared store is a shared blast radius. Mitigated by making promotion
  **explicit + curated** (nothing auto-promotes) and keeping working knowledge scoped by default.
- **Risk — concurrency**: a shared skills/knowledge DB read by N live agents needs WAL + read
  concurrency (SQLite FTS5 handles concurrent reads; writes are promotion-only, low-frequency).

## Implementation slices

1. **Per-store tier flag** — `scope: shared | scoped` per store; resolver skips `scope_leaf` for
   shared. Ship with **skills → shared**, everything else scoped. (Smallest viable "feature, not
   bug": a fleet shares its skills library, keeps goals/knowledge/checkpoints private.)
2. **Workspace CLI** — `new/ls/run/rm` over `(config-dir, instance-id, port)` + a `workspaces/`
   convention + `workspace.yaml` registry; `--bundle` wires ADR 0040.
3. **Read-through layering + promotion** — `scope: layered` for skills/knowledge (base+overlay
   reads), `skill promote` / `knowledge_promote`. The full shared-brain model.
4. **Console surface** — a workspace switcher + a "commons vs private" indicator in the UI
   (optional, after the CLI proves the model).

Slices 1–2 deliver the whole user-visible story (shared skills + named, isolated, bundle-installable
agents); 3–4 deepen it.
