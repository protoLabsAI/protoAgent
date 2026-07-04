# 0072 — Fleet seed: declare a team in config (team-via-config)

- Status: Proposed
- Date: 2026-07-04
- Builds on: ADR 0025 (unified delegate registry), 0040 (plugin bundles), 0041
  (workspaces & tiered stores), 0042 (fleet supervisor & unified console).
- Related: #1778 (hub→member delegation timeout — orthogonal, but a team is only
  as useful as its delegation).

## Context

ADR 0042 gave us a **fleet**: many named agents on one host, supervised, each an
isolated workspace (ADR 0041), created from an **archetype** bundle (ADR 0040) that
seeds the member's `SOUL.md` + plugin stack + config. ADR 0025 gave the lead a
`delegate_to` roster to hand work to those members.

Every one of those pieces is imperative. You create a member by hand — the console
new-agent picker or `POST /api/fleet {name, bundle}` — then hand-edit its config,
then hand-add it to the lead's `delegates:`. A **working team** (a lead plus the
specialists it delegates to) is therefore *not* a config-as-code artifact. It can't
be baked into an image, versioned, diffed, or stood up on a fresh host with one
command. It exists only as accreted runtime state in a sandbox volume.

We hit this dogfooding a content team: a lead (`jon`) with two members — a content
drafter and a frontend/design-system engineer — that jon delegates to. Standing it
up meant: create two blank members in the console, hand-write two `SOUL.md`s, hand-
wire each member's plugins/MCP/knowledge, then hand-add two `a2a` delegates with
descriptions and auth. None of it is reproducible; a volume wipe loses the whole
team. Meanwhile `PROTOAGENT_SEED_CONFIG` (ADR 0041) already seeds the *lead's* config
on first boot — but there is no equivalent for the *team around it*.

The question ADR 0042 left open: **how does an operator stand up a ready-to-go
protoAgent team from config alone?**

## Decision

A **fleet-seed manifest** — a baked, declarative team roster reconciled on first
boot, "seed-not-force" (like `PROTOAGENT_SEED_CONFIG`): it never clobbers live edits,
so an operator can still add/adjust members in the console afterward.

`PROTOAGENT_SEED_FLEET` points at a `fleet.seed.yaml`. On boot, the supervisor
reconciles the live fleet to it: create-from-archetype any missing member, apply the
declared config overrides, start the ones marked `autostart`, and **auto-derive the
lead's `delegates:`** from the roster. Bake the manifest (plus the member archetypes)
into an image and `docker compose up` stands up the entire team — lead, members,
delegate wiring, and (optionally) a shared knowledge commons — with no console steps.

```yaml
# fleet.seed.yaml  (PROTOAGENT_SEED_FLEET)
version: 1
commons:                         # optional shared "team knowledge" (ADR 0041 layered)
  scope: layered
  path: /sandbox/commons
members:
  - name: cindi
    archetype: { url: https://github.com/protoLabsAI/content-drafter-archetype, ref: v0.1.0 }
    autostart: true
    config:                      # per-member overrides merged onto the archetype's config
      knowledge: { scope: layered }
    delegate:                    # how the LEAD should expose this member
      expose: true
      description: >
        Content drafter — hand her a brief (surface, thesis, beats, grounding
        facts); she pulls the studio-brand voice and drafts on-brand.
  - name: matt
    archetype: { url: https://github.com/protoLabsAI/frontend-ds-archetype, ref: v0.1.0 }
    autostart: true
    delegate:
      expose: true
      description: Frontend / design-system / a11y engineer — delegate DS & component work.
```

## Design details

### A. Reconciliation (the seed step)

Runs once at boot, after the lead's own `ensure_live_config` (ADR 0041), gated the
same way (`PROTOAGENT_HEADLESS_SETUP`). For each declared member:

1. **Absent** → `workspace new --bundle <archetype>` (ADR 0042's create path), then
   merge `config:` overrides onto the archetype's seeded config, then start if
   `autostart`.
2. **Present** → leave it. Seed-not-force: never overwrite a member's live config or
   `SOUL.md`. (Re-seed a member by removing its workspace, exactly like re-seeding the
   lead is removing the config volume.)

Idempotent: a second boot with an unchanged manifest is a no-op. Manifest *changes*
add new members but do not mutate existing ones (see Open questions on drift).

### B. Delegate auto-derivation

The lead's `delegate_to` roster (ADR 0025) is generated from the roster: each
`expose: true` member becomes an `a2a` delegate — `name` = member name, `url` =
`http://127.0.0.1:{port_base+N}/a2a` (the supervisor already owns port allocation,
ADR 0042), `description` from the manifest (falling back to the archetype's
`archetype.blurb`), `auth` = the fleet's shared bearer via `credentialsEnv`. This is
the piece that today is pure hand-work and the easiest to get subtly wrong (missing
description → the lead doesn't know when to route; missing auth → 401).

### C. Archetypes carry the member; the manifest carries the team

No new packaging primitive. A member's persona + tools stay in an **archetype bundle**
(ADR 0040/0042) — reusable across teams. The manifest is only the *roster over* those
archetypes plus team-level wiring (delegates, commons). `content-drafter` and
`frontend-ds` archetypes are as reusable as `pm-stack`.

### D. Shared knowledge commons (optional)

A `commons:` block sets every member (and the lead) to a layered/shared store (ADR
0041) at one path — the team's "collective psyche." Requires one embed model across
the fleet (already enforced by the commons stamp). Durable team canon is promoted in;
live data is never seeded (it drifts) — that's a plugin/tool concern, not knowledge.

### E. Env + image shape

`PROTOAGENT_SEED_FLEET=/opt/<agent>/seed/fleet.seed.yaml`, baked beside
`PROTOAGENT_SEED_CONFIG`. The archetype bundles are `plugin install`ed into the image
at build (ADR 0040) so reconciliation is offline-clonable. Net effect: a team image =
lead config seed + fleet seed + N archetype bundles.

## Consequences

- **A team becomes a config-as-code artifact**: versioned, diffable, reproducible on
  any host, and documentable ("stand up a protoAgent team via config").
- **Two teams, two manifests, one mechanism**: the content team (lead + drafter +
  frontend) and an engineering team (a PM lead + specialist members) stop being
  bespoke builds. The PM/portfolio orchestration lives in a plugin on top of the same
  fleet+delegate substrate, not a fork.
- Re-seeding stays coarse and predictable (remove the workspace → recreate from
  archetype), consistent with the config-volume re-seed model.

## Open questions

1. **Drift on manifest change** — adding a member is easy; changing an existing
   member's archetype/config is deliberately a no-op (seed-not-force). Do we want an
   opt-in `--reseed <member>` that recreates one member, or leave it fully manual?
2. **Per-member secrets** — overrides can reference `*_env` / `credentialsEnv`, but a
   team with distinct per-member credentials needs a story for routing secrets to the
   right member's `secrets.yaml` at seed time.
3. **Delegation cost** — teams multiply delegate traffic; #1778 (the 60s synchronous
   `SendMessage` timeout) should land first or the team's delegations silently fall
   back on any non-trivial member turn.
4. **Remote members** — the manifest assumes local (loopback) members. A `url:` escape
   hatch for a member that lives on another host (ADR 0042 remote members) is a natural
   extension but out of scope here.

## Alternatives considered

- **Console-only (status quo)** — not reproducible; the problem statement.
- **A "bundle of bundles"** — bundles (ADR 0040) install a *plugin stack*, not a team
  roster with delegate wiring and per-member identity. Overloading them would conflate
  "capability" with "org chart."
- **Hand-scripted create calls** — a shell script hitting `POST /api/fleet` works but
  isn't declarative, isn't seed-not-force, and re-runs are not idempotent.
