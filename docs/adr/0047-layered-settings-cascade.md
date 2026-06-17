# ADR 0047 — Layered settings cascade (App→Host→Agent, per-field, `Field.scope`)

- **Status:** Accepted (2026-06-10; decisions locked in the operator walkthrough — see §2.1).
  **Host-file scoping ratified _per-hub_ on 2026-06-16 (#1077)** — `host_config_path()` is
  `scope_leaf`'d; the original §D2 unscoped-`data_home()` (per-physical-box) proposal is superseded.
- **Date:** 2026-06-10
- **Deciders:** Josh Mabry; protoAgent maintainers
- **Tags:** config, settings, fleet, host, architecture, cascade, schema-driven
- **Related:** extends the FIELDS single-source refactor (B1 — `config_to_dict`
  is `FIELDS`-driven, `from_dict` is the dict-level parse seam); builds on
  [ADR 0004](./0004-multi-instance-data-scoping.md) (per-instance scoping /
  `scope_leaf`), [ADR 0019](./0019-plugin-config-settings-secrets.md) (plugin
  config / secrets routing), [ADR 0025](./0025-unified-delegate-registry-and-panel.md)
  (delegates over a2a/openai/acp), [ADR 0041](./0041-workspaces-and-tiered-stores.md)
  (workspace == agent), and [ADR 0042](./0042-fleet-supervisor-unified-console.md)
  (fleet supervisor, slug routing, model-only inheritance, remote members).

> Proposed. Today an agent's config is a single per-agent
> `langgraph-config.yaml` parsed into a flat dataclass, where each attribute
> falls back to the dataclass default (`section.get(key, cls.<field>)`,
> `graph/config.py`). That is already a **two-layer** per-field merge
> (YAML-or-default). Box-wide concerns (ports, discovery, supervisor warm
> policy, bind interface, data root) have **no shared home** — they are scattered
> env-var / CLI reads at the call site. And new-agent creation eagerly **copies**
> the host's `model:` block into each leaf (`manager._overlay_model`), a one-shot
> graft, not a live inheritance. This ADR adds a real, declarative **middle
> layer** and makes the merge **three-layer, nearest-wins, per field** — like git
> `config` `system → global → local`, but per FIELD — by extending the just-merged
> `Field` single-source with one attribute (`scope`), reusing the existing
> `_deep_merge` primitive and the untouched `from_dict` parser, with **zero
> migration**.

## 1. Context & Problem statement

### 1.1 Where config comes from today (the two-layer floor)

There is exactly one seam that decides where an agent's config lives: the
`PROTOAGENT_CONFIG_DIR` env var, read in `graph/config_io.py:54` (`_live_config_dir`).
From it derive the per-agent paths, all `scope_leaf`-wrapped per instance:

- `CONFIG_YAML_PATH = scope_leaf(_LIVE_CONFIG_DIR/langgraph-config.yaml)` (`graph/config_io.py:80-81`)
- `SECRETS_YAML_PATH = scope_leaf(_LIVE_CONFIG_DIR/secrets.yaml)` (`graph/config_io.py:89-90`)
- `SETUP_MARKER_PATH` (`graph/config_io.py:121-122`)

The host process runs with no `PROTOAGENT_CONFIG_DIR` ⇒ config dir =
`REPO_ROOT/config`; a fleet workspace agent runs with
`PROTOAGENT_CONFIG_DIR=<workspace>` + `PROTOAGENT_INSTANCE=<id>` set by
`workspaces.manager.run_exec` (`graph/workspaces/manager.py:294-296`), so its
leaf reads at `<workspace>/<id>/langgraph-config.yaml` (double-scoped, seeded
from the manager-written `<workspace>/langgraph-config.yaml` by
`ensure_live_config`, `graph/config_io.py:140-174`).

The load path is two lines, repeated at boot, hot-reload, and `--setup`
(`server/agent_init.py:71-72`):

```python
ensure_live_config()
STATE.graph_config = LangGraphConfig.from_yaml(CONFIG_YAML_PATH)
```

`from_yaml` (`graph/config.py:490-506`) is a thin wrapper: read the YAML, load
the sibling `secrets.yaml`, then delegate to `from_dict(data, secrets=…,
config_dir=…)`. `from_dict` (`graph/config.py:508+`) maps each leaf to a flat
dataclass field with the dataclass default as the fallback:
`section.get(key, cls.<field>)`. **That per-attr `.get(key, default)` is
structurally a two-layer (YAML-or-default) per-field merge today** — the floor
the cascade builds on.

### 1.2 The FIELDS single-source the cascade extends

`config_to_dict` (`graph/config_io.py:229-304`) is now `FIELDS`-driven (B1):
section (A) walks `graph/settings_schema.py:FIELDS` and emits each `key → attr`
into a nested dict (secrets redacted to `""`); section (B) layers the non-FIELDS
legacy keys (mcp / knowledge.db_path / skills / plugins / researcher); section
(C) layers the ADR-0019 plugin sections. The `Field` dataclass
(`graph/settings_schema.py:21-33`) already carries `restart: bool` — a per-field
boolean that both the schema endpoint and the UI key off. **`Field.scope` is the
natural extension, mirroring `restart` exactly.**

### 1.3 The gap

Box-wide concerns have no declarative home and no per-field merge:

- **Bind interface** — `--host` / `PROTOAGENT_HOST` (default `127.0.0.1`), `server/__init__.py:332`.
- **Port / port base** — `--port` → `STATE.active_port` (`server/__init__.py:356`); `PORT_BASE=7870` + `_pick_port` (`graph/workspaces/manager.py:22,113-128`).
- **Discovery** — port range + mDNS `_protoagent._tcp` + tailnet (`graph/fleet/discovery.py:32,212-241`); advertised at boot (`server/__init__.py:563`).
- **Supervisor warm policy** — `PROTOAGENT_FLEET_MAX_WARM`, `PROTOAGENT_FLEET_WARM_GRACE` (`graph/fleet/supervisor.py:248-251,285`).
- **Data root** — `data_home()` (`paths.py:64-67`), `PROTOAGENT_AUTO_SCOPE`.

These are read directly at the call site, with no shared file and no per-field
override. Only `auth.token` and `runtime.autostart_on_boot` are box-ish fields
that exist in `FIELDS` today. The cascade's Host/Machine layer is exactly the
home these concerns lack.

## 2. The decided model (settled — not relitigated here)

- **Three cascade layers, nearest wins, PER FIELD:** App/World defaults →
  Host/Machine → Agent (leaf). Per-FIELD override (like git
  `system → global → local`), **not** section-level.
- The **Host/Machine layer** (ports, discovery, supervisor, auth posture, data
  root) is **separate** from the slug=`host` AGENT. The slug=`host` agent is the
  running process viewed through the fleet/proxy lens (`graph/fleet/supervisor.py:173-194`,
  `host: True`; `graph/fleet/proxy.py:52-64`, `"host" → STATE.active_port`) — it
  is a leaf agent, not a layer. The Host layer is the box-wide settings **shared
  by every agent this machine owns**.
- **workspace == agent** — a workspace IS an agent's home (ADR 0041); there is no
  separate workspace layer.
- **Remote agents are NOT a layer.** A remote sibling has its own App→Host→Agent
  cascade on its own machine; locally it is only a delegate reference
  (handle / URL / token / advertised skills) viewed via the slug-routing proxy.
  The Host layer is inherently local-machine-scoped.
- Built on the FIELDS single-source: `config_to_dict` is `FIELDS`-driven,
  `from_dict` is the dict-level parse seam, `Field.scope` is the extension.

> Note: the protoagent forks (gina / roxy / protoTrader) are being **retired**,
> so fork-cleanliness is no longer a design constraint — the Host file living
> outside the tracked `config/` tree carries no fork-merge cost.

## 2.1 Decisions locked (operator walkthrough, 2026-06-10)

These settle the open questions the draft left to the operator. They **supersede**
the corresponding items in §7.

1. **`Field.scope` is git-style advisory, not ownership.** `scope` is a field's
   *home / default* layer (where the UI edits it and where a shared default
   lives), but **any field stays overridable at a lower layer** (`system → global
   → local`). No `locked` state is introduced now — none of the current 47 FIELDS
   needs it (the lock candidates — bind interface, port range — aren't FIELDS yet;
   D8). Add `locked:bool` only when a concrete box-policy field demands it.

2. **Per-field `host` vs `agent` assignment (the rest = `agent`; App = dataclass
   defaults only, no writable App file):**

   | `host` field | rationale |
   |---|---|
   | `model.api_base`, `model.provider` | the LiteLLM **gateway** is machine infra |
   | `model.name` | the box's default model — this IS today's "model-only inheritance", now via the cascade; **agents override freely** (their own `model.name` in the leaf wins) |
   | `routing.aux_model`, `routing.fallback_models` | gateway routing |
   | `prompt_cache.enabled/ttl/warm.enabled/warm.interval_seconds` | cache tier is gateway/deployment-dependent |
   | `telemetry.enabled`, `telemetry.retention_days` | observability is machine-wide |
   | `identity.org` | white-label org branding is deployment-wide (`identity.name`/`operator` stay per-agent) |

   Everything else is `agent`: `identity.name/operator`, `model.temperature/max_tokens/max_iterations`,
   `agent_runtime` (each agent picks **native or `acp:*`** independently), `operator_mcp.tools`,
   `goal.*`, `compaction.*`, `execute_code.*`, `knowledge.*`, `skills.*`, `middleware.*`,
   `checkpoint.*`, `operator.allowed_dirs`, `runtime.autostart_on_boot`. Secrets
   (`model.api_key`, `auth.token`) stay **leaf-only** (D5). An agent overriding
   `model.name` or running on ACP pairs with its own leaf-scoped key.

3. **Host file is `scope_leaf`'d (per-hub), NOT unscoped `data_home()`.** This
   rides the same isolation as `fleet.json` / `remotes.json` (hub-scoped, #813) —
   one-hub-per-box ≡ per-box; multiple instances on one box stay isolated (no #706
   regression). True multi-tenant on one box uses **containers** (ADR 0004's
   recommended boundary), so each tenant gets its own `data_home` → its own host
   policy. (Corrects D2's earlier unscoped-`data_home()` proposal — **ratified #1077**;
   §D2 + the Status line above are reconciled to per-hub.)

4. **Host-change propagation = banner now, broadcast later.** A host-layer change
   re-merges for free on each co-located agent's next reload (`_reload_langgraph_agent`
   re-runs `from_yaml` → the cascade). v1: the host console shows "host config
   changed — agents apply on next reload." Live broadcast over the event bus
   (ADR 0039) is a follow-up. (Resolves §7 item 6.)

5. **Remote-agent boundary is concrete (verified against #839).** A remote member
   is a `remotes.json` entry `{opaque-id, name, url, token?}` next to `fleet.json`
   (hub-scoped, #813), reverse-proxied by URL with its bearer replacing the
   browser's at the proxy boundary (`graph/fleet/proxy.py` `_target_for_slug`).
   "The remote agent itself is untouched" — `activate` no-ops, no start/stop. Its
   App→Host→Agent cascade resolves **on its own machine**; we never load remote
   config into ours. (D7, now grounded in #839 rather than the abstract delegate ref.)

6. **Open follow-ups still genuinely undecided** (carried to implementation): the
   D8 env-knob promotion order + env-vs-file precedence; corrupt/unparseable
   `host-config.yaml` handling (must not crash boot — degrade to App+Agent with a
   warning); whether `validate_flat` rejects a wrong-layer save at the API boundary
   (defense-in-depth, recommended yes); and the one-time treatment of existing
   eager model-copy blocks (`manager._overlay_model`) when new-agent creation flips
   to inherit-from-host.

## 3. Decision

### D1 — `Field.scope`: one attribute, mirroring `restart`

Add to the `Field` dataclass (`graph/settings_schema.py:21-33`):

```python
scope: str = "agent"   # "app" | "host" | "agent" — the field's home layer
```

- **Default `"agent"`** (the leaf): an un-tagged field behaves exactly as today
  (lives in the per-agent `langgraph-config.yaml`). Adding the attribute is a
  **no-op for all 40+ existing Fields** until each is deliberately re-tagged.
- `scope` declares the field's **home layer** — the layer a save targets by
  default and the layer the "Override here" affordance writes to. It does **not**
  mean "read only from that layer": a `"host"` field still cascades
  App-default → Host → (and may be overridden at the Agent leaf, git-local-over-
  global). `scope` (where you may set it) is orthogonal to `source` (the nearest
  layer that actually set it).
- **App/World is read-only from the UI** — it is materialized implicitly as the
  dataclass defaults (`build_schema` already reads `type(config)().attr` as the
  field's `default`, `graph/settings_schema.py`). Almost nothing is tagged
  `"app"`; the App layer is the dataclass-default floor, not a writable file.
- Derive a `_HOST_KEYS = {f.key for f in FIELDS if f.scope == "host"}` index next
  to the existing `_BY_KEY` / `_SECRET_KEYS` (`graph/settings_schema.py`). The
  Host doc is **filtered to only those keys** before merge — the enforcement that
  a Host file can never smuggle in a leaf-only field (plugins / secrets / legacy
  section-B keys), even if hand-edited.

`Field.scope` is a bare `str` (matching the `restart: bool` house style), with a
documented allowed-set and the derived `_HOST_KEYS` index. It can tighten to a
`Literal` later with no migration.

### D2 — Storage layout: three files, three resolvers

**App / World** — NO new file. Source = the dataclass defaults already
materialized everywhere (`type(config)()`). The bundled
`CONFIG_EXAMPLE_PATH = _BUNDLE_CONFIG_DIR/langgraph-config.example.yaml`
(`graph/config_io.py:62`) is the **seed template + comment carrier only** — the
cascade does NOT read it as a live layer. `app_doc = {}`, relying on the
dataclass defaults that `from_dict` already falls back to.

**Host / Hub** (NEW — per-hub, `scope_leaf`'d; **ratified #1077**):

```python
HOST_CONFIG_PATH = host_config_path()   # = scope_leaf(data_home() / "host-config.yaml")
```

`host_config_path()` (`infra/paths.py`) `scope_leaf`-wraps `data_home()/host-config.yaml`,
so the host file is **per-hub** — riding the same isolation as `fleet.json` / `remotes.json`
(#813) — not one-per-physical-box. In the usual one-hub-per-box deployment that's equivalent
to per-box; multiple hubs on one box stay isolated (no #706 regression), and true multi-tenant
on one box uses containers (ADR 0004), each with its own `data_home`. The file is **OPTIONAL**:
absent ⇒ the Host layer is empty ⇒ App defaults show through. A `PROTOAGENT_HOST_CONFIG` env
override (points at a FILE, mirroring `PROTOAGENT_CONFIG_DIR`'s role for the leaf) handles the
read-only frozen desktop sidecar.

> **Ratified per-hub (#1077).** Supersedes this section's original proposal of an *unscoped*
> `data_home()/host-config.yaml` (one-per-physical-box, NOT `scope_leaf`'d). Decision item 3
> (§"Decisions") corrected it; the shipped `host_config_path()` and the Global ▸ Configuration
> console copy both reflect the ratified per-hub scope. Other "unscoped `data_home()` host file"
> phrasings remaining below are pre-ratification context.

**Agent / Leaf** (UNCHANGED): the existing
`CONFIG_YAML_PATH = scope_leaf(_LIVE_CONFIG_DIR/langgraph-config.yaml)`
(`graph/config_io.py:81`), addressed via `PROTOAGENT_CONFIG_DIR`. The host
process reads `REPO_ROOT/config/langgraph-config.yaml`; a fleet workspace agent
reads `<workspace>/<id>/langgraph-config.yaml` (seeded from `<workspace>/…`).
`secrets.yaml` stays the per-agent `0600` sibling overlay, OUTSIDE the cascade
(D5). Plugin config (ADR 0019) stays agent-local, OUTSIDE per-field scope (D6).

### D3 — Loader merge: merge the dicts, then ONE `from_dict`

The cascade is a **pre-processing step in front of an untouched parser**. In
`from_yaml` (or a new `from_layers` classmethod it delegates to,
`graph/config.py:490-506`):

```python
app_doc   = {}                                          # dataclass defaults ARE the App layer
host_doc  = read(HOST_CONFIG_PATH) filtered to _HOST_KEYS   # only host-scoped keys participate
agent_doc = read(CONFIG_YAML_PATH)                      # the existing scope_leaf'd leaf
merged    = _deep_merge(_deep_merge(deepcopy(app_doc), host_doc), agent_doc)  # Agent wins last
secrets   = _load_secrets_doc(agent_dir)                # LEAF secrets only
return cls.from_dict(merged, secrets=secrets, config_dir=agent_dir)
```

- `_deep_merge` (`graph/config_io.py:219-226`, "src wins on leaf conflicts")
  already exists. **Nearest-wins is just merge order** (App, then Host, then
  Agent last). No new merge primitive.
- `from_dict` is fed a merged dict; the runtime config **shape is unchanged** —
  every `.get(key, default)`, the secret overlay
  (`api_key = secret_api_key or model.get("api_key", cls.api_key)`,
  `graph/config.py`), and `_resolve_plugin_config` (`graph/config.py:44-74`) are
  untouched. The cascade adds **zero new parse logic** and cannot drift from the
  YAML-or-default path. (This is why we merge **dicts**, not parsed dataclasses —
  see Alternative A.)
- **Only host-scoped keys are pulled from the Host file** — everything else in
  `host-config.yaml` is filtered out (and logged), so an operator cannot
  accidentally pin an agent-scoped field machine-wide.
- The merge happens at the `from_yaml`/`from_dict` step (`server/agent_init.py:72`),
  so every downstream store / middleware sees the already-merged config. The
  hot-reload path (`_reload_langgraph_agent`, `server/agent_init.py:952-957`)
  re-runs the same path, so reload stays consistent for free.

**List semantics:** the deep merge is a key-merge; a leaf list **replaces** a
parent list (it does not concatenate). For a per-field nearest-wins cascade this
is correct (a leaf `egress.allowed_hosts` overrides the host default) and matches
how `from_dict` already treats lists (`list(... or [])`). It must be documented
so no one expects list-union.

### D4 — Settings UI + write path

**Read (schema endpoint).** `build_schema` (`graph/settings_schema.py:222`) is
threaded the three raw layer dicts (App defaults — already free as `default`;
Host doc; Agent doc) in addition to the merged `STATE.graph_config` it sees
today, and emits per field:

- `scope` — the home layer (from `f.scope`).
- `value` — the effective/merged value (unchanged).
- `source` — `"app" | "host" | "agent"`, the nearest layer that actually **set**
  the key (a layer "set" it only if that YAML file contains the key).
- `layers: {app, host?, agent?}` — the per-layer values so "Reset" can preview the
  inherited value.

`GET /api/config` → `config_to_dict` (`operator_api/config_routes.py:50-56`) and
`GET /api/settings/schema` (`config_routes.py:142-153`) are the read endpoints;
`config_to_dict` is already `FIELDS`-driven, so per-layer serialization reuses the
same loop.

**Save (write path).** The wire request grows from `{updates}` to
`{updates, layer="agent", deletes=[]}` (`SettingsUpdateRequest`,
`operator_api/config_routes.py:38`). `POST /api/settings`
(`config_routes.py:155-169`) still does `validate_flat` → `nest_updates`, then
calls `_apply_settings_changes(config=nested, layer=layer, deletes=deletes)`
(`server/agent_init.py:1189-1245`). The `layer` param resolves to a target path
and threads it through the seams that **already take a `path` arg**:
`load_yaml_doc(path=target)` / `apply_updates_to_yaml` / `save_yaml_doc(path=target)`
(`graph/config_io.py:177,197,307`):

- Agent → `CONFIG_YAML_PATH` (the scope_leaf'd per-instance leaf, current behavior).
- Host → `HOST_CONFIG_PATH` (the per-hub `scope_leaf`'d `host_config_path()`, #1077;
  created lazily on first "save to Host layer").

The save's **actual target layer is decided per request** (the panel's render
context — Agent view vs. a Host/Machine view — supplies `layer`, defaulting to the
field's `scope`). This is what makes it a true per-FIELD git-style cascade rather
than a fixed routing table: "Override here" can write a host-default field down to
the leaf. `validate_flat` should additionally reject saving a leaf-only key against
the Host layer (defense in depth, trivial given `_BY_KEY[k].scope`).

**"Reset to inherited" = DELETE the key from the layer's file** so the parent
shows through. This is the **one genuinely new write primitive**:
`apply_updates_to_yaml` (`graph/config_io.py:307-329`) only merges, never deletes
— add a sibling `pop_keys_from_yaml(doc, keys)` that pops from the right layer
file while preserving ruamel comments/order on surviving keys (and round-trips
through the `yaml.safe_dump` fallback at `save_yaml_doc`).

**UI render.** `SettingInput` stays layer-agnostic (it switches on
`field.type` only, `apps/web/src/settings/SettingsCategory.tsx:258-341`). The
inheritance affordance lives in `SettingRow`, parallel to the existing `restart`
pill (`SettingsCategory.tsx:264`): `key ∈ dirty` → "overridden here (pending)";
else `source === <viewLayer>` → "overridden here" + a "Reset to inherited"
control; else → an "inherited from {source}" badge + an "Override here" control.
The displayed value is always `field.value` (effective), so the cascade is
invisible to the input — only the badge + reset/override are new. The dirty-only
save (`SettingsCategory.tsx:61-94`) gains a target `layer`; reset is a second
delete mutation. `SettingsField` (`apps/web/src/lib/types.ts:142-155`) extends
with `scope`, `source`, and `layers?`.

`restart` / `restart_keys` semantics are **orthogonal and unchanged** — every
save still hot-reloads (`server/agent_init.py:1189-1245`), and the reload now
re-runs the three-layer merge; the `restart` flag stays advisory.

### D5 — Secrets stay leaf-only (outside the cascade)

`secrets.yaml` is a separate, per-agent, gitignored, `0600` file — a parallel
store, not part of the FIELDS dict cascade. Secret fields are redacted to `""` in
`config_to_dict` (`graph/config_io.py:247`) and routed by `split_secret_updates`
/ `secret_paths()` (`graph/config_io.py:102-114,337-380`). `split_secret_updates`
→ `save_secrets` always writes the **Agent's own** `secrets.yaml` regardless of
the `layer` param.

Sharing secrets up a cascade is a security regression (one shared readable file
compromises every agent the box owns). Secrets compose with the cascade on a
different axis: the cascade resolves non-secret fields across App/Host/Agent on
the **secret-stripped** dict (the YAML is stripped on every save), then
`from_dict` overlays the **leaf** `secrets.yaml` onto the merged dict. Resolution
order: **cascade-the-field-first, then overlay-the-leaf-secret**. So a
Host-supplied `model.api_base` pairs with the agent's own `model.api_key` from its
leaf `secrets.yaml`. The `manager._overlay_model` gateway-credential copy at
agent-create (`graph/workspaces/manager.py:227-248`) is the existing precedent:
config can come from the host, but the secret is the agent's own file. **There is
no host-level secrets file in v1.**

### D6 — Plugin config stays agent-local (outside per-field scope)

Plugin config (ADR 0019) is section-shaped opaque data layered **outside** the
FIELDS loop (`config_to_dict` section (C), `graph/config_io.py:287-303`), so
there is no per-field handle to attach `scope` to — honoring the decided
"per-FIELD, not section-level" rule is impossible for it without a parallel
scoping system. The set of installed plugins is itself per-workspace (each agent
gets its own `PROTOAGENT_PLUGINS_DIR` / `plugins.lock`,
`graph/workspaces/manager.py:294-295`), so a Host-layer plugin section would
frequently reference a plugin the leaf hasn't installed. `_resolve_plugin_config`
runs last inside `from_dict` against the merged dict, but because
`plugins.enabled/disabled/dir` are leaf-only (the Host doc is filtered to exclude
them via `_HOST_KEYS`), it resolves against the agent's own plugin set exactly as
today. **Plugin config stays leaf-only in the first cut.** A shared plugin default
is a deliberate later follow-up (ride the App/Host YAML as a whole `<section>:`
block resolved by precedence **before** `_resolve_plugin_config`, not via
`Field.scope`).

### D7 — Remote agents are a delegate reference, not a layer

What is stored for a remote is one entry in the top-level `delegates:` list,
parsed to `{name, type:"a2a", description, url, auth_scheme, auth_token}`
(`plugins/delegates/adapters.py:53-82,138-163`). There is **no copy** of the
remote's model / knowledge / middleware — those live on the remote's own machine,
under its own App→Host→Agent cascade. The slug-routing reverse proxy resolves
**local ports only** (`graph/fleet/proxy.py:52-64`: `"host" → STATE.active_port`,
any other slug → a local workspace's supervisor port at `127.0.0.1:<port>`); a
remote on another box has no local port and is reached only via `delegate_to` over
A2A to its URL — never proxied, never merged. Discovery
(`/api/fleet/discover`, `operator_api/fleet_routes.py:36-49`;
`graph/fleet/discovery.py:115-125,212-241`) is read-only candidate surfacing →
an `upsert_delegate`, never an auto-create or config copy. **Therefore the Host
layer is inherently local-machine-scoped** — exactly why the host file resolves
from the **unscoped** `data_home()`, not the `scope_leaf`'d `workspaces_root()`.

This cascade also **generalizes and should eventually replace** the eager
model-only copy at agent-create (`manager._overlay_model`,
`graph/workspaces/manager.py:227-248`; `operator_api/fleet_routes.py:98-105`,
"Carry the host's MODEL only") for **local** agents — that copy is the de-facto
"model-only inheritance" this ADR makes a live per-field merge. It correctly does
not touch remotes.

### D8 — Host knobs are promoted into FIELDS incrementally, with an env-fallback bridge

The Host layer only pays off once the box-wide knobs are in `FIELDS` and tagged
`scope="host"`. Most live in env/CLI today, not `FIELDS`. Promote ~5 of them
(bind interface, port base, discovery policy, supervisor warm cap/grace, data
root) **incrementally**. Each promoted field ships **with** an env-var fallback so
existing env-configured boxes keep working: the host-layer resolver falls back to
the existing env var when `host-config.yaml` omits the key
(`PROTOAGENT_HOST` → bind, `PROTOAGENT_FLEET_MAX_WARM` → warm cap, …). The shim is
**load-bearing for zero-migration** and must ship in the same PR as the
promotion, not after.

Shipping order keeps each PR independently shippable and the leaf path green
throughout:

1. `Field.scope` + the host file + the read-merge with **no new host fields**
   (pure refactor — leaf behavior byte-for-byte identical, the Host layer is empty).
2. Schema `source`/`layers` + the UI badges/reset (Agent leaf view, badge-aware).
3. Promote env/CLI host knobs into `FIELDS` as `scope="host"` (with env fallback)
   + add the Host/Machine settings view.

**Status — all three slices SHIPPED.** Slices 1–2 landed with the settings-IA fold
(PR #925). Slice 3 (this) promotes the box-runtime knobs `network.bind`,
`fleet.port_base`, `fleet.discovery.port_min/port_max/mdns`, and
`fleet.warm.max/grace_seconds` into `FIELDS` (`scope="host"`, section "Fleet" →
category System, so they surface in Settings ▸ Host / App ▸ Host config). The
env-fallback bridge lives in `from_dict` as `section.get(key, _env_default(ENV, default))`,
fixing **Open decision #2 to file > env > default** (operator, 2026-06-11): the file/UI
value wins, the `PROTOAGENT_*` env var is the zero-migration fallback consulted only when
the merged dict omits the key, and an explicit `--host` flag wins over both. The four call
sites (`server.__init__` uvicorn bind, `manager._pick_port`, `fleet.discovery`, and
`fleet.supervisor.max_warm`/`_warm_grace_seconds`) read the resolved config via the lazy
`runtime.state.STATE` accessor, falling back to env in a CLI/no-config context. Data-root /
`PROTOAGENT_AUTO_SCOPE` is intentionally NOT promoted (it resolves where `host-config.yaml`
itself lives — chicken-and-egg; stays env/CLI).

## 4. Migration / back-compat

**ZERO migration. Zero new bytes on disk for any existing deployment.**

- Today's single `config/langgraph-config.yaml` **IS** the Agent leaf already
  (`CONFIG_YAML_PATH` resolves there for the host process with no
  `PROTOAGENT_CONFIG_DIR`). It keeps being read verbatim.
- `host-config.yaml` is **absent on every existing box** ⇒ the Host layer
  contributes nothing ⇒ the cascade collapses to App-defaults → Agent-YAML = the
  **exact** two-layer `data.get(key, cls.default)` behavior `from_dict` already
  implements. The merge is a **no-op** when the host file doesn't exist; the
  byte-for-byte runtime config is identical.
- `ensure_live_config` (`graph/config_io.py:140-174`) returns early when
  `CONFIG_YAML_PATH.exists()` — an existing config is never re-seeded or
  rewritten. Adding the Host layer touches none of that path. (That existing
  "scoped instance inherits the unscoped base" seeding is the spiritual ancestor
  of this cascade — a copy-on-first-run that the cascade turns into a live
  per-field merge.)
- No existing file moves, is renamed, or changes schema. The only new artifact is
  the **optional** `host-config.yaml`, created lazily on the first "save to Host
  layer" (or by hand) by operators who want box-wide overrides.
- `Field.scope` defaults to `"agent"`, so adding the attribute is a
  zero-behavior-change no-op until fields are deliberately re-tagged.
- Promoting an env/CLI host knob into `FIELDS` is zero-migration **only with the
  env-fallback bridge (D8)** shipped alongside — otherwise an existing
  `PROTOAGENT_FLEET_MAX_WARM` box would silently lose its setting.

## 5. Consequences

- **A real, declarative Host/Machine layer** — box-wide knobs (ports, discovery,
  supervisor, data root, bind interface) get a single home and a UI surface for the
  first time, instead of scattered env/CLI reads.
- **Per-field, git-style inheritance** — `model.name` can be host-shared while
  `model.temperature` is agent-tuned (same section, different layers); "inherited
  from Host", "overridden here", and "Reset to inherited" become first-class UI
  affordances.
- **Tiny structural footprint** — one new `Field` attribute, one unscoped file
  resolver, one merge call in front of an **untouched** `from_dict`, one new
  delete-key YAML helper. No new runtime, no second parse path (the exact drift B1
  just eliminated), and the cascade composes correctly with the secret overlay and
  `_resolve_plugin_config` purely by ordering.
- **Hard leaf-only guarantee** — `_HOST_KEYS` filtering means plugins / secrets /
  legacy keys can never be set from the Host file, even by a hand-edit; an
  unknown/typo'd host key is dropped (and logged), not honored.
- **Cross-agent propagation is per-process** — a Host-file edit by one agent
  affects other co-located agents only on **their** next reload/restart (the
  existing per-process reload model). The "restart required" banner must
  communicate **machine-wide** impact for host fields, not just this-process.
- **One Host layer per box** — a scoped multi-tenant hub on one box shares ONE
  `host-config.yaml` across tenants (the host file is unscoped). This is by design
  ("Host = local-machine-scoped"); a tenant cannot have its own host defaults
  unless it sets `PROTOAGENT_HOST_CONFIG`.
- **Containers** — `data_home() = /sandbox` is per-container, so `host-config.yaml`
  is per-container (each container IS a box) — correct. Bare-metal multi-instance
  shares `~/.protoagent/host-config.yaml` — also correct for machine-wide.
- **A stale host-scoped key in a leaf YAML is a silent no-op** — once a key is
  tagged `scope="host"`, an existing leaf value for it is ignored (host fields are
  sourced only from `host-config.yaml`/env). A boot warning is recommended to avoid
  the footgun.
- **The model-only copy is superseded** — new local-agent creation should flip
  from `manager._overlay_model` copy to inherit-from-host (a separate, loosely
  coupled change so create-time regressions are isolated).

## 6. Alternatives considered

- **A. Parse each layer to a `LangGraphConfig`, then merge dataclasses
  (non-default-wins).** Rejected. "Set vs defaulted" is unrecoverable after
  `from_dict` (a layer that omits `model.temperature` and one that sets it to the
  default are indistinguishable), forcing sentinels everywhere; it runs `from_dict`
  + `_resolve_plugin_config` + the secret overlay **three times** against partial
  layers, then reconciles; and it creates a **second merge codepath** that can
  drift from the YAML-or-default path. The dict merge keeps ONE parse path.
- **B. Section-level scope (tag whole YAML sections to a layer).** Rejected by the
  decided model ("per-FIELD, not section-level"), and it breaks the realistic
  `model.api_base`-host-shared-but-`model.temperature`-per-agent case (same
  section, different layers).
- **C. No per-edit `layer` — route every save strictly by `Field.scope`.**
  Rejected: loses the git "override a system value at local scope" affordance (you
  couldn't pin a host-default field at one agent). The explicit `layer` is one
  field on the request and unlocks the core use case.
- **D. "Reset to inherited" writes the parent value into the leaf** (instead of
  deleting the key). Rejected: it **freezes** the inherited value (future host
  changes won't propagate) and litters the leaf with redundant keys. Deleting the
  key (true inheritance) is correct; the new `pop_keys_from_yaml` is small.
- **E. Host file under the scoped `workspaces_root()/host.yaml`** (sibling of
  `fleet.json`). Rejected as primary: `workspaces_root()` is `scope_leaf`'d
  (`graph/workspaces/manager.py:47,51`) ⇒ a scoped hub gets
  `~/.protoagent/<iid>/workspaces/host.yaml`, i.e. one host file **per hub-instance**,
  contradicting "Host = local-machine-scoped." Use only if "host" should mean
  per-hub rather than per-box (a real semantic fork — Open decision 1).
- **F. Host file inside `_LIVE_CONFIG_DIR`** (`<config_dir>/host-config.yaml`).
  Rejected: for a workspace agent `_LIVE_CONFIG_DIR` IS that agent's own dir, so the
  "shared" file would be per-agent, defeating the layer's purpose.
- **G. Host file at `REPO_ROOT/config/host.yaml`** (next to the example).
  Rejected: pollutes the tracked bundle dir with per-deployment state (the exact
  anti-pattern `langgraph-config.yaml` was pulled OUT of) and breaks the read-only
  frozen desktop sidecar. `data_home()` is writable, untracked, and genuinely shared.
- **H. App layer as a live read of `CONFIG_EXAMPLE_PATH`** (a third live doc)
  rather than the dataclass defaults. Deferred: the dataclass defaults already ARE
  the App layer everywhere (`build_schema` reads `type(config)()` as `default`);
  reading the `.example` live double-sources defaults and risks the file and the
  dataclass disagreeing. Keep `.example` as the seed template only.
- **I. Synthesize the Host layer from env vars at boot** (no physical file).
  Rejected as the durable design (it IS the status quo the cascade replaces), but
  **adopted as the compat bridge** (D8): the host-layer resolver falls back to the
  existing env var when the host file omits the key, so promoting env knobs into
  `FIELDS` is itself zero-migration.
- **J. Cascade-merge plugin config and/or secrets too** (full uniformity).
  Rejected per D5/D6: plugin config is section-shaped with no per-field handle and
  an agent-local plugin set; sharing secrets up a layer is a security regression.
- **K. `Field.scope` as a typed `Literal`/enum** rather than a bare `str`.
  Deferred: `restart: bool` set the bare-field house style and `validate_flat`
  doesn't enforce enums on metadata; a `str` + the documented allowed-set + the
  derived `_HOST_KEYS` index is sufficient and can tighten later with no migration.

## 7. Open decisions for the operator

1. **HOST = box or hub?** The recommended unscoped `data_home()/host-config.yaml`
   makes "host" = physical box (all co-located instances share it). Alternative E
   (`workspaces_root()/host.yaml`) makes "host" = hub-instance (each scoped hub gets
   its own). The decided model says "local-machine-scoped" — read as box. Confirm a
   scoped hub is **not** entitled to its own host policy distinct from a sibling hub
   on the same box (those that need isolation can set `PROTOAGENT_HOST_CONFIG`).
2. **Which env/CLI knobs become Host FIELDS in the first promotion, and does env
   override the file or vice-versa?** Candidates: `PROTOAGENT_HOST`/`--host`,
   `PORT_BASE`, discovery port range + mDNS toggle, `PROTOAGENT_FLEET_MAX_WARM` /
   `PROTOAGENT_FLEET_WARM_GRACE`, `data_home` / `PROTOAGENT_AUTO_SCOPE`. Git-style
   puts persistent config above ambient env; ops tooling often expects env to win.
   Decide the precedence ruling explicitly.
3. **Ship the env-fallback bridge (D8) in the same PR as field promotion, or accept
   a one-time "move your env settings into host-config.yaml" migration note?**
   Recommended: ship the shim (keeps promotion zero-migration).
4. **Host secrets posture.** Confirm there is NO host-level secrets file in v1:
   `host-config.yaml` carries only non-secret host config; a host-scoped non-secret
   field pairs with a leaf secret (cascade-field-first, then overlay-leaf-secret).
   Any host-shared credential is explicitly out of scope for v1.
5. **Does a `"host"`-scoped field allow a LEAF override, or is it host-only?**
   Recommendation: leaf-may-override (git local-over-global). If some host fields
   must be **locked** box policy (e.g. egress/auth posture the agent can't
   override), `scope` needs a third state or a separate `locked: bool`. Defer unless
   a concrete locked field is named.
6. **Cross-agent propagation of a host-file change.** Each process picks it up only
   on its own reload/restart. v1 options: (a) just banner it; (b) signal the
   supervisor to mark warm agents stale; (c) broadcast a reload over the event bus
   (ADR 0039). Recommend (a) for v1, (c) as follow-up. Confirm.
7. **Host settings view gating — who may edit the Host layer?** Proposal: only the
   slug=`host` console exposes a Host settings surface; workspace-agent consoles show
   host fields as read-only "inherited from Host (edit on the host console)". Do
   workspace agents get a deep-link to the host console?
8. **Stale host-scoped key in a leaf YAML — warn or silent?** Once a key is tagged
   `scope="host"`, an existing leaf value is ignored (correct nearest-wins-by-scope,
   but a silent no-op). Recommend a boot warning. Confirm acceptable.
9. **`PROTOAGENT_HOST_CONFIG` override** — OK to mint a new env var mirroring
   `PROTOAGENT_CONFIG_DIR` (but pointing at a FILE, not a dir) for the read-only
   frozen desktop sidecar where `data_home()` may not be the right home? (Recommended
   yes.)
10. **Workspace double-scoping — confirm the canonical Agent-layer file.** A
    workspace agent reads from `<ws>/<id>/langgraph-config.yaml` (`CONFIG_YAML_PATH`)
    while the manager writes the seed at `<ws>/langgraph-config.yaml` (seeded down by
    `ensure_live_config`). Confirm the cascade's Agent layer and the save target are
    the nested `<ws>/<id>/` file (the runtime read path), with `<ws>/…` as the
    one-time seed.
11. **"Reset to inherited" on a leaf-only field** (no host/app value beyond the
    dataclass default): reset should fall back to the App default (the dataclass
    value via `from_dict`'s `.get(key, cls.default)`). Confirm the UI labels this
    "inherited from App (default)".
12. **Is the App/World layer ever writable from the host console** (editing
    `langgraph-config.example.yaml`)? Recommend NO — keep App a read-only floor.
    Confirm.
13. **Migration of the existing eager model-only copy** (`manager._overlay_model`):
    when new-agent creation flips to inherit-from-host, existing agents already carry
    a COPIED `model:` block in their leaf. Leave those as explicit leaf overrides
    (they'll show "overridden here"), or offer a one-time "reset model to inherit from
    host" migration?
14. **Defense-in-depth at the API boundary** — should `validate_flat` reject saving
    a leaf-only key against the Host layer (and vice-versa) in addition to the
    `_HOST_KEYS` read-time filter? Recommend yes (trivial given `_BY_KEY[k].scope`).
