# 0065 — Two-tier instance paths (box / instance) + single resolution rule

- Status: Accepted
- Date: 2026-06-30
- Supersedes: the path-scoping mechanism of [ADR 0004](0004-multi-instance-data-scoping.md); the
  `PROTOAGENT_CONFIG_DIR` / "a workspace *is* a config dir" parts of
  [ADR 0041](0041-workspaces-and-tiered-stores.md) and
  [ADR 0042](0042-fleet-supervisor-unified-console.md)
- Amends: [ADR 0047](0047-layered-settings-cascade.md) (the Host layer file is now box-shared)

## Context

protoAgent located on-disk state through **two roots scoped by two different rules**:

- the **agent config** (`langgraph-config.yaml`, `secrets.yaml`, `.setup-complete`, `theme.json`)
  lived under a *config dir* (`PROTOAGENT_CONFIG_DIR` or `REPO/config`), scoped by `_config_scope`,
  which **skipped** instance-scoping whenever `PROTOAGENT_CONFIG_DIR` was set; and
- the **data stores** + the **Host cascade layer** lived under the *data home* (`/sandbox` or
  `~/.protoagent`), scoped by `scope_leaf` (the instance id inserted as the leaf's parent).

Those rules collided exactly where it mattered most: a **fleet member** launches with *both*
`PROTOAGENT_CONFIG_DIR=<workspace>` and `PROTOAGENT_INSTANCE=<id>`, so the two scopings disagreed,
and a destructive self-heal (`_reset_double_scoped_config`) existed only to delete the wreckage —
which at one point deleted a live instance's gateway key. Path locations were also frozen as
**import-time module constants**, computed before the instance id was even known, so config-leaf and
data-store paths could scope differently within one process (the chicken-and-egg that forced an
`_seed_instance_env` re-scope step at boot). The Host layer — meant to be *machine-wide* — was
wedged into the data home and `scope_leaf`'d per instance (#813), contradicting its purpose.

## Decision

One model, three directory tiers that mirror the ADR-0047 cascade (App → Host → Agent), resolved
**once** from the environment into a frozen, injectable `infra.paths.InstancePaths`:

- **App** = `app_root/config` — read-only bundle seed (example YAML, `SOUL.md` source, presets).
  Live config is **never written into the repo tree** again.
- **Box** = `box_root` — **machine-shared, never scoped**: `host-config.yaml` (the Host cascade
  layer, now genuinely box-wide), `commons/` (shared skills), `.instances/` heartbeats,
  `.data-version`, response cache.
- **Instance** = `instance_root` — **per-agent; it IS the scoped leaf** (`scope_leaf` is never
  applied to it — the single fact that deletes the entire double-scope bug class): `config/`,
  `plugins/`, and every per-instance store (`checkpoints.db`, `knowledge/`, `memory/`, `scheduler/`,
  `inbox/`, `activity/`, `telemetry.db`, `skills.db`, `audit/`, `a2a-*.db`, `tasks/`, `workflows/`,
  `workspace/`, …).

**One resolution rule** — two orthogonal knobs, so `PROTOAGENT_HOME` relocates *only* the instance
tier and the box tier stays shared (fleet members under their own `HOME` still inherit one
machine-wide Host layer + commons):

```
box_root      = PROTOAGENT_BOX_ROOT  else  data_home()        # /sandbox if a dir else ~/.protoagent
instance_root = PROTOAGENT_HOME                               # terminal: the dir IS the root
              | box_root / PROTOAGENT_INSTANCE                # a named instance under the box
              | box_root / "default"                          # neither set → "default"
instance_id   = PROTOAGENT_INSTANCE | basename(PROTOAGENT_HOME) | "default"
```

**Identity comes from the environment only** — never config-file content — so a correctly-scoped
config is read on the first try (no chicken-and-egg, no `_seed_instance_env`). Every instance is
scoped (the default is just the named instance `default`); the legacy *unscoped* branch is gone.

Deleted: `_config_scope`, `_reset_double_scoped_config`, the import-time path constants,
`_seed_instance_env`, `PROTOAGENT_CONFIG_DIR`, `PROTOAGENT_AUTO_SCOPE`, and `scope_leaf` itself.
The fleet registry (`fleet.json` / `workspaces/`) stays under the **hub's** `instance_root`, not the
box tier — so a booting member reads its own empty registry and `shutdown_all` stays "hub-only by
construction" (it cannot SIGTERM its siblings).

### Deployment shapes

| Shape | env | box_root | instance_root |
|---|---|---|---|
| Local default | — | `~/.protoagent` | `~/.protoagent/default` |
| Dev sandbox | `PROTOAGENT_INSTANCE=dev` | `~/.protoagent` | `~/.protoagent/dev` |
| Docker | `PROTOAGENT_HOME=/sandbox` | `/sandbox` | `/sandbox` |
| Desktop | `PROTOAGENT_HOME=<app-data>` | `~/.protoagent` | `<app-data>` |
| Fleet member | `PROTOAGENT_HOME=<ws>` + `PROTOAGENT_INSTANCE=<id>` | `~/.protoagent` | `<ws>` |

### Seamless upgrade (no back-compat in the runtime, but no stranded data)

The runtime carries **no** legacy dual-mode logic. A single, self-contained, deletable
`migrate_legacy_layout()` runs at first boot under the new layout: when the new config is absent it
**copies** (never moves; `copy2` preserves `secrets.yaml`'s 0600) the old-layout config + secrets +
setup-marker + theme into `instance_root/config`, and carries the **default** instance's data stores
from their old `box_root/<store>` locations into `box_root/default/<store>`. Idempotent,
non-destructive, logged. Scoped sandboxes (e.g. `dev`) re-initialise (they are wipeable test state).

## Observability

`config explain` (CLI `python -m server config explain` + `GET /api/config/explain`) prints the
instance id, both roots, every resolved path, and the per-field cascade provenance with secrets
redacted — the supported way to answer "where did my config/key go", replacing the deleted
self-heal.

## Consequences

- The double-scope bug class is **structurally impossible** (one scoping input; `instance_root` is
  the leaf). The fleet member `CONFIG_DIR`+`INSTANCE` collision cannot recur.
- The Host layer is **box-shared** (reverses #813): two co-located hubs now read one machine-wide
  `host-config.yaml`. This is the intended "machine-wide host" semantics — a Settings save of a
  host-scoped field is box-wide.
- Per-field cascade semantics and `Field.scope` from ADR 0047 are unchanged; only the Host file's
  *location* moves (box-shared, no `scope_leaf`).
- A workspace is now simply an `instance_root` (the ADR-0041 "workspace == config dir" model
  collapses); fleet member launch sets `PROTOAGENT_HOME=<ws>` rather than `PROTOAGENT_CONFIG_DIR`.
