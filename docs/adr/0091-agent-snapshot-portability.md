# ADR 0091 — Agent snapshot portability: a declarative, secret-free bundle (not a raw state dump)

**Status:** Accepted

**Implementation:** **Slice 1 (export) shipped** — `#2103`, `graph/snapshot_op.py` +
`POST /api/agent/export` + `protoagent agent export`. Slices 2–4 (import, knowledge seed, UX)
remain: `#2104`, `#2105`, `#2106`. Slice plan in Consequences. The runtime knowledge-import we already ship in
`claude-bridge-plugin` (memory/CLAUDE.md → `knowledge_add`) is a working prototype of the D4
knowledge-seed half.

**Relates to:** [ADR 0004](0004-multi-instance-data-scoping.md) / [ADR 0065](0065-two-tier-instance-paths.md)
(the instance root a snapshot captures), [ADR 0047](0047-layered-settings-cascade.md) (the
config layer that is snapshotted), [ADR 0080](0080-external-secrets-manager.md) (the secret
boundary), [ADR 0027](0027-install-plugins-from-git-url.md) / [ADR 0058](0058-runtime-plugin-install-frozen-app.md)
(the pinned plugin install the snapshot re-runs), [ADR 0042](0042-fleet-supervisor-unified-console.md) /
[ADR 0083](0083-cowork-mode-archetype.md) (the archetype/bundle scaffold a snapshot rehydrates through).

## Context

An agent is one **instance root** (`infra/paths.py`, ADR 0004/0065): under it sit the declarative
config (`config/langgraph-config.yaml`, `config/SOUL.md`, `config/skills/`), the pinned plugin set
(`plugins.lock` + `plugins/`), and ~15 runtime sqlite stores (knowledge, memory, goals, watches,
tasks, checkpoints, telemetry, scheduler, a2a, inbox, …). There is **no export/backup/snapshot
feature today** — the only adjacent primitives are `snapshot_soul` (SOUL history) and
`_config_files_to_snapshot()` (a config-write rollback net), neither portable.

The need: *"export a zip of the agent (minus secrets) and easily spin up an agent from that frozen
snapshot."* Two naive readings both fail:

1. **Raw instance-root zip.** Heavy, opaque, and — fatally — **secret-laden**: `secrets.yaml`,
   `.fleet-token`, hashed device tokens, and secret *values* buried across ~15 sqlite DBs would all
   have to be scrubbed, which can't be guaranteed. It is also **version-brittle** — a v0.105 sqlite
   snapshot may not open on a later schema. This is the `docker commit` / committed-`.tfstate`
   anti-pattern: an unauditable materialized-state dump used where a recipe belongs.
2. **Copy the existing `workspaces/manager.create()` `from_config` clone.** That path copies
   `secrets.yaml` **verbatim** — the opposite of secret-free.

The industry has settled this: every reproducibility-focused system ships a small **declarative
definition** and treats the materialized snapshot as a heavy, brittle, secret-laden thing you do
NOT distribute — Docker (Dockerfile vs `commit`), Terraform (`.tf` vs `.tfstate`), Ollama Modelfile,
Letta Agent File, VS Code Profiles, chezmoi. The acceptance bar is 12-Factor Config's litmus:
**the artifact could be made public without compromising a single credential.**

## Decision

Adopt an **agent snapshot = a declarative bundle + optional seed**, exported through protoAgent's
existing secret-strip machinery and rehydrated through its existing scaffold. The zip carries a
*recipe*, not a state dump.

### D1 — The snapshot is a declarative bundle, reusing the `protoagent.bundle.yaml` shape

The exported artifact is a directory/zip whose manifest (`agent.snapshot.yaml`) is modeled on the
existing bundle format (`graph/plugins/installer.py` `load_bundle`): the SOUL reference, the config
(secret-stripped), the pinned plugin set (`plugins.lock` — url + resolved SHA, already secret-free),
subagents, MCP servers, and the archetype reference. Plugins are re-installed by **pinned SHA** and
the model is referenced by its **gateway alias** (portable, not baked). The bundle schema is already
protoAgent's "declarative agent recipe" — the snapshot is its export form.

### D2 — Secret-free by the 12-factor litmus, with a `required_secrets` schema

The exporter runs the config doc through the existing `strip_secrets_from_doc()` /
`secret_paths()` machinery (`graph/config_io.py`) **and additionally nulls `mcp.servers[].env` /
`.headers`** — which are stored inline in the main config and are NOT covered by `SECRET_PATHS`
(the sharpest leak risk). It **excludes** `secrets.yaml`, `devices.json`, and `.fleet-token`
entirely. Following Letta Agent File (nulls all secrets on export, keeps structure) and the A2A
Agent Card (`securitySchemes` declares *which* auth is needed without the value), the snapshot
carries a **`required_secrets` schema** — the names/descriptions of every credential the target must
re-provide (from `secret_paths()` + each plugin manifest's `secrets:` + MCP keys), so import can
**prompt** for them rather than silently producing a broken rehydrate. The bar: the snapshot could
be pushed to a public gist without leaking a credential.

### D3 — Rehydrate through the existing `create()` scaffold, as a new secret-free path

Import feeds the snapshot into a `graph/workspaces/manager.py::create()`-style scaffold — which
already stands up a fresh instance root from a config base + persona + bundle install + config/MCP
defaults, with identity re-stamping. This is a **new, secret-free entry** into that scaffold, NOT the
existing `from_config` clone (which copies secrets). After the declarative install, import **prompts
for the `required_secrets`** using the same surface as plugin `needs_config` / the setup wizard.

### D4 — Stateless definition always; stateful seed opt-in and operator-reviewed

The **definition** (SOUL, config, plugins, skills, subagents) always travels — it is the
reproducible, shareable core, and skills already re-seed from `SKILL.md` dirs each boot. **Runtime
history** (checkpoints, telemetry, metrics, activity, inbox, a2a, scheduler, background) is seeded
**empty** — a snapshot yields a *fresh* agent, not a resumed one. **Knowledge (and optionally
memory)** is an **opt-in seed**: exported as domain-tagged source docs and re-ingested via
`knowledge_add` on the target (re-embedded against its own gateway) — the durable form of the
claude-bridge memory import. Note that *secret-free ≠ safe-to-publish* for knowledge: it can hold
sensitive project detail, so the knowledge seed is a distinct, operator-reviewed axis from the
credential boundary.

### D5 — Snapshot (portability) is not backup (disaster recovery)

This ADR is scoped to **portability/clone** — declarative, secret-free, cross-machine. **Backup**
(same-machine, same-version disaster recovery that DOES include history and secrets) is a different,
simpler feature — an encrypted `tar` of the instance root — and is explicitly out of scope here. The
IaC lesson (Terraform config-vs-state, Docker Dockerfile-vs-commit) is precisely to **not** use the
DR snapshot as the portability format.

## Consequences

- **A shareable, reviewable, secret-free "agent recipe."** An operator can export protoEngineer (or
  any agent), commit it, hand it to a teammate, or seed a fresh instance — and the artifact is safe
  to store anywhere. `claude-bridge-plugin`'s roadmap'd "export a translated bundle" becomes a
  consumer of this format rather than a parallel mechanism.
- **~80% reuse.** The exporter builds on `strip_secrets_from_doc`/`secret_paths`/`config_to_dict`;
  the rehydrator builds on `manager.create()`. The genuinely new work is the manifest schema, the
  MCP-env scrub, the `required_secrets` inventory, and the import prompt.
- **Fresh, not resumed.** Rehydration deliberately drops conversation history/checkpoints. If
  resume-with-history is ever wanted, that is the separate D5 backup feature, not this.
- **Trade-off accepted:** a manifest+seed re-derives runtime state and can't perfectly reproduce a
  hand-mutated instance — which is the point (cattle, not pets). Raw-state fidelity is sacrificed for
  portability, auditability, secret-safety, and version tolerance.

### Slice plan

1. ~~**Export**~~ — **SHIPPED (#2103)**: `protoagent agent export` + `POST /api/agent/export`
   (`?dry_run` for review-before-download) → zip carrying `agent.snapshot.yaml`, `SOUL.md`,
   `skills/`, and a `REVIEW.md` disclosure.

   **Correction to this ADR's own plan:** it said to reuse `strip_secrets_from_doc`. The
   implementation deliberately does not, and the reason generalizes. That function is the
   **save** path and fails **open** by design — if relocating a value into `secrets.yaml`
   fails, it leaves the secret inline rather than lose the operator's credential (#1645) —
   and it *writes* to `secrets.yaml` as a side effect. Both are correct for a file staying
   on the box and wrong for an artifact leaving it. Export uses a pure, fail-**closed**
   redactor (`snapshot_op.redact_config_for_export`) that reuses `secret_paths()` for the
   key inventory only. **A redactor's failure mode has to match its artifact's destination.**

   Two further things only a run against a real agent surfaced:
   - **The inventory must walk `secret_paths()`, not the config's inline keys.** A
     correctly-configured agent keeps credentials in `secrets.yaml`, so an inline-only walk
     skipped exactly the real ones — `model.api_key`, the gateway key the agent cannot run
     without, was absent from `required_secrets` entirely and import would have stood up a
     dead agent without ever asking. The overlay is read (read-only) to answer *whether* a
     credential is set; no value is read out of it.
   - **A second, pattern-level layer is needed** (`export_op.redact` over every free-text
     value that ships). The structural strip is key-shaped and cannot see a token pasted
     into a plugin's text field or into `SOUL.md`. Its findings are reported, split between
     *credential-shaped* (treat as exposed — rotate at the source) and *machine-local*
     (a home path — nothing to rotate, re-point on the target); conflating the two sends an
     operator hunting a breach that never happened.
2. **Import / rehydrate** — feed the snapshot into a `create()`-style scaffold; **prompt for
   `required_secrets`**. (Reuses `manager.create()`, `ensure_live_config`/`ensure_live_soul`.)
3. **Knowledge seed (opt-in)** — export knowledge domains as seed docs; re-ingest via `knowledge_add`
   on import. (The durable version of the claude-bridge memory import.)
4. **Polish** — console/desktop "duplicate agent from snapshot" UX; converge the claude-bridge
   "export a translated bundle" roadmap onto this format.
