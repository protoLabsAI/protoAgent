# 0040 — Plugin bundles (install a curated set of plugins as one)

- Status: Accepted
- Date: 2026-06-09
- Builds on: ADR 0027 (git-installable plugins — `plugin install <url>` + `plugins.lock` + `sync`).

## Context

ADR 0027 made each plugin a standalone, git-URL-installable repo, pinned in a committed
`plugins.lock`. That is the right *atom* — independent versioning, independent release cadence, one
repo per capability. But a working agent is usually a **stack** of several plugins that are tested
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
id: pm-stack
name: Project Manager Stack
description: Board orchestration + browser + delegate spine.
plugins:
  - { id: delegates,     builtin: true }                       # ships with protoAgent
  - { id: project_board, url: …/projectBoard-plugin,  ref: v0.1.0 }
  - { id: agent_browser, url: …/agent-browser-plugin, ref: v0.1.0 }
enabled: [delegates, project_board, agent_browser]
config:
  agent_browser: { panel_mode: full }
```

`plugin install <bundle-url>` detects the bundle manifest and **fans out to per-plugin `install()`**
for each member — so every member is allow-list-checked and pinned in `plugins.lock` *exactly* as a
direct install would be (`by: "bundle:<id>"` records provenance). The bundle itself is recorded under
a `bundles:` section of the lock for traceability + reproducible re-install. `builtin: true` members
are skipped (they ship with the host). `plugin sync` already re-clones the locked set, so bundle
members re-sync for free — no bundle-specific sync path.

Crucially, **install ≠ enable ≠ trust still holds** (ADR 0027): a bundle install only puts code on
disk and pins it. The `enabled` list and `config` are *returned as suggestions* (printed by the CLI),
never written to the live config — turning the stack on remains the operator's explicit decision.

## Consequences

- **Composition over duplication** — bundles reference the standalone plugin repos; no code is copied
  or moved. Plugins keep their own repos, CI, and release cadence; a bundle just pins a *tested combo*.
- **One install entry point** — `plugin install <bundle-url>` brings up the whole stack; one repo to
  share, one ref to bump when the combo is re-validated.
- **Lock is still the source of truth** — members appear in `plugins.lock` like any plugin; the
  `bundles:` entry is additive provenance. Existing `list`/`sync`/`uninstall` are unaffected.
- **Trust boundary unchanged** — no auto-enable, no auto-config, no auto-dep-install. A bundle can't
  silently turn anything on.
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
