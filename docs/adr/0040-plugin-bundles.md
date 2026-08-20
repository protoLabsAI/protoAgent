# 0040 — Plugin bundles (install a curated set of plugins as one)

- Status: Accepted
- Date: 2026-06-09 (amended 2026-08-14 — manifest fields + install-time seeding grew
  through July: `mcp:`/`secrets:` (#2011/#2041/#2118), `archetype:` (ADR 0042/0100,
  #2715), `verified_against` (ADR 0049); the console/ops install path now enables and
  seeds by default, superseding the original suggestions-only claim — see the amendment
  note in Decision.)
- Builds on: ADR 0027 (git-installable plugins — `plugin install <url>` + `plugins.lock` + `sync`).

> **Terminology** *(amended 2026-08-19)*. A *bundle* is the mechanism this ADR defines.
> A published bundle repo that ships an `archetype:` block is an **archetype repo**
> (`cowork-archetype`, `social-archetype`, …) — see ADR 0100. The old term "stack" is
> **retired**; the former `*-stack` repos were renamed (GitHub redirects keep old URLs
> working). Code, API, and UI say `bundle`.

## Context

ADR 0027 made each plugin a standalone, git-URL-installable repo, pinned in a committed
`plugins.lock`. That is the right *atom* — independent versioning, independent release cadence, one
repo per capability. But a working agent usually composes **several plugins** that are tested
*together*: e.g. a "project manager" agent = a board-orchestration plugin + a browser plugin + the
delegate spine. Today, standing that up means hand-installing each URL at the right ref and
hand-assembling the `plugins.enabled` list and recommended config — error-prone, and impossible to
*share* as a single, versioned thing.

We want a way to publish and install a **bundle**: a named, versioned, curated set of plugins. The
question is whether a bundle is a *monorepo of plugin code*, or a *reference manifest* over the
existing standalone plugin repos.

## Decision

A **bundle is a reference repo**, not a code monorepo. A bundle repo's root holds a
`protoagent.bundle.yaml` (in place of a `protoagent.plugin.yaml`) that *names* a set of plugin repos
to install together, plus a suggested enable list + config:

```yaml
id: project-manager-archetype
name: Project Manager
description: Board orchestration + browser + delegate spine.
plugins:
  - { id: delegates,     builtin: true }                       # ships with protoAgent
  - { id: project_board, url: …/projectBoard-plugin,  ref: v0.1.0 }
  - { id: agent_browser, url: …/agent-browser-plugin, ref: v0.1.0 }
enabled: [delegates, project_board, agent_browser]
config:
  agent_browser: { panel_mode: full }
# Fields added after this ADR's first writing (full worked example:
# examples/bundles/template/protoagent.bundle.yaml):
#   verified_against: <core version the pins were last verified on>   # ADR 0049
#   mcp:      [{ template, inputs }]      # MCP servers to seed (#2011, ADR 0083 D5)
#   secrets:  [{ key, label, … }]         # declared secrets to prompt for/seed (#2041)
#   archetype: { label, icon, blurb, soul/soul_preset, tier, requires, requires_tools }
#                                         # starter-type self-registration (ADR 0100)
```

`plugin install <bundle-url>` detects the bundle manifest and **fans out to per-plugin `install()`**
for each member — so every member is allow-list-checked and pinned in `plugins.lock` *exactly* as a
direct install would be (`by: "bundle:<id>"` records provenance). The bundle itself is recorded under
a `bundles:` section of the lock for traceability + reproducible re-install. `builtin: true` members
are skipped (they ship with the host). `plugin sync` already re-clones the locked set, so bundle
members re-sync for free — no bundle-specific sync path.

**Amendment (2026-08): install ≠ enable ≠ trust now holds on the CLI only.** As first
written, the `enabled` list and `config` were *returned as suggestions*, never written to
the live config. Three paths have since deliberately moved to enable-and-seed — installing
there IS the consent (ADR 0071's trust-by-default posture):

- **console/ops** (`POST /api/plugins/install` → `ops.plugins.install_and_activate`):
  enables the bundle's curated set, seeds `config:` as defaults (operator values never
  clobbered, #1350), seeds `mcp:` servers + supplied `secrets:` (#2118), hot-reloads.
- **workspace create** (`manager.create(bundle=…)`): same seeding into the new agent,
  atomic (full rollback on failure).
- **CLI `plugin install`** remains fetch-only — suggestions printed, nothing enabled —
  the strict-mode escape is `PROTOAGENT_PLUGIN_INSTALL_NO_ENABLE=1` on the console path.

## Consequences

- **Composition over duplication** — bundles reference the standalone plugin repos; no code is copied
  or moved. Plugins keep their own repos, CI, and release cadence; a bundle just pins a *tested combo*.
- **One install entry point** — `plugin install <bundle-url>` brings up the whole set; one repo to
  share, one ref to bump when the combo is re-validated.
- **Lock is still the source of truth** — members appear in `plugins.lock` like any plugin; the
  `bundles:` entry is additive provenance. Existing `list`/`sync`/`uninstall` are unaffected.
- **Trust boundary** — *(2026-08)* the console/ops and workspace-create paths auto-enable
  and seed (installing is the consent, ADR 0071); the CLI stays fetch-only. Deps are
  never auto-installed anywhere.
- Minor: a bundle install is non-atomic across members (if member 3 fails, members 1–2 are already
  installed). Acceptable — they're independently pinned and a re-run with `--force` is idempotent.

## Options considered

- **Monorepo of plugin code** (one repo, `plugins/<name>/` dirs). Atomic versioning + one CI, but it
  fights the ADR 0027 one-repo-per-plugin model, forces a shared release cadence, and would need the
  installer to learn sub-directory/multi-manifest installs. Rejected.
- **Bare shareable `plugins.lock`** (hand someone a lock + `plugin sync`). Works with zero new code,
  but the lock is a deployment's *whole* set with no name/description/enable/config metadata, and
  bundles can't compose. Kept as the underlying mechanism; rejected as the user-facing bundle.
- **Reference-manifest bundle** (this decision). Thin, composable, reuses every downstream primitive.
