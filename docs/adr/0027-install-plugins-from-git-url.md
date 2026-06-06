# 0027 — Install plugins from a git URL (shareable plugin repos)

Status: **Accepted** (sliced)

## Context

Plugins ([ADR 0018](./0018-plugin-surfaces-routes-subagents.md) backend,
[0019](./0019-plugin-config-settings-secrets.md) settings,
[0026](./0026-plugin-contributed-console-surfaces.md) console surfaces) are a
full extension surface — but today a plugin must live **in-repo** (`plugins/`).
There's no way to make one in its own GitHub repo and share it. ComfyUI
popularized git-URL "custom nodes"; we want the same for protoAgent — author a
plugin repo, install it by URL — without ComfyUI's "clone = arbitrary code runs"
safety posture. This is the long-deferred Slice 5 (registry/marketplace) of
[ADR 0001](./0001-extensibility-and-plugin-architecture.md).

Two facts shape the design:
- **The seam already exists.** `graph/plugins/loader.py::_plugin_roots()` already
  discovers an **external** plugins dir (`<config_dir>/plugins`, outside the repo),
  and discovery reads `protoagent.plugin.yaml` as **data without importing** the
  plugin. So *fetching* a plugin never executes its code; only *enabling* it does.
- **In-process plugins share the interpreter.** Installing one means trusting its
  code — exactly like adding a pip dependency. You **cannot** sandbox it with a
  per-plugin venv (the code runs in the main interpreter). True isolation of
  *untrusted* code is what **MCP** already provides (out-of-process, declared
  tools). So git-URL plugins are best framed as **trusted, reviewed code**.

## Decision

A `plugin install <git-url>` flow (CLI **and** console) that clones into the
external plugins dir, pins to a resolved commit, records a lockfile, surfaces the
manifest + capabilities for review, and **never auto-enables or auto-runs code**.

### D1 — Trust model: install ≠ enable ≠ trust

Git-URL plugins are **trusted, in-process code** (you reviewed it, like a pip dep).
The three steps are distinct:
- **Install** = `git clone` + checkout pinned ref → code on disk. No import, no
  execution (deps are **not** pip-installed — D4).
- **Discover** = read the manifest (data). No import.
- **Enable** = `plugins.enabled` → `register()` runs. **This** is the trust decision.

For **untrusted** third-party code, use **MCP** (out-of-process, sandboxable), not
a git plugin. Stated prominently in the docs + the install review.

### D2 — Install location + reproducibility (lockfile, pinned SHA)

Clone into `<config_dir>/plugins/<id>/` (already on `_plugin_roots`; gitignored
from the fork). Record every install in a committed **`plugins.lock`**:
`{id, source_url, requested_ref, resolved_sha, installed_at, by}`. Always pin to a
**resolved commit SHA** — never silently track a moving branch. `plugin sync`
re-clones the exact set from the lock (reproducible forks / CI / containers).

### D3 — Source posture: any URL + mandatory review gate (+ optional allowlist)

Any git URL is allowed, but **every install requires an explicit confirm** showing:
source URL, resolved SHA, manifest (id/name/version/description/repository), declared
capabilities (network/fs/secrets), and what it contributes (tools/views/routes/
subagents). A fork can lock down with `plugins.sources.allow: [github.com/org/*]`
(refuse anything off-allowlist). **Default: open + gated** (builder-friendly, never
silent).

### D4 — Dependencies: declare-only, explicit install (no surprise code-exec)

Manifest gains `requires_pip: ["pkg>=x"]`. **`plugin install` fetches code only — it
does NOT pip-install** (pip runs arbitrary `setup.py`/build code, which would defeat
"install ≠ execute"). The operator installs deps as a **separate explicit step**
(`plugin install-deps <id>`, or the shown `pip install` line) after reviewing them.
Missing deps → the plugin fails to import on **enable** with a clear "declared deps
not installed: …" message, not a cryptic ImportError.

### D5 — Capabilities surfaced + audited (enforcement iterates)

Before enable, surface declared capabilities (network hosts, filesystem scope,
secrets requested, tools/views/routes added) for operator review. Audit-log
install / enable / disable / uninstall (url, sha, operator, time) to the existing
audit log. **No hard in-process enforcement in v1** — honest: you can't sandbox
in-process Python. A per-plugin egress allowlist + fs fencing is a documented
fast-follow; untrusted code → MCP (D1).

### D6 — Lifecycle: CLI **and** console

- **CLI** (`python -m server plugin …`): `install <url> [--ref <tag|sha>] [--enable]`,
  `list`, `enable/disable <id>`, `uninstall <id>`, `sync` (from lock),
  `install-deps <id>`. `--enable` is opt-in; bare install does **not** enable (D1).
- **Console** (Settings → **Plugins**): paste URL → review card (manifest + caps +
  resolved SHA) → install → enable toggle → uninstall. Mirrors the delegates panel.

### D7 — Manifest additions (all data, all optional)

`requires_pip: [..]` (D4); `repository:` / `homepage:` (provenance, shown in review);
`min_protoagent_version:` (compat — warn/refuse if the host is older).

### D8 — Integrity rails

- Pin to resolved SHA (D2); `--ref` accepts tag/branch/sha → resolved + recorded.
- Clone `--depth 1` at the ref; **no submodules** by default (a vector).
- Validate the manifest (id matches dir, required fields) before accepting; reject
  a repo with no `protoagent.plugin.yaml` (not a plugin).
- Refuse to overwrite a built-in or existing id without `--force` (no silent
  shadowing).
- Uninstall removes the dir + the lock entry. Cloned-but-disabled plugins are inert
  (discovery is data-only).

### D9 — Slices

- **PR1:** manifest additions (`requires_pip`/`repository`/`min_protoagent_version`)
  + installer core (clone → resolve SHA → validate manifest → write `plugins.lock`)
  + `plugin list/install/uninstall/sync` CLI (no enable, no dep auto-run). Tests.
- **PR2:** console **Plugins** panel + the install/review/uninstall API (paste URL →
  review card → install → enable/disable → uninstall). e2e.
- **PR3:** `requires_pip` + `install-deps` + missing-dep diagnostics; capability
  review surfacing + audit logging; `plugins.sources.allow` enforcement; docs
  (`guides/plugin-registry.md` "install + publish a plugin" + the untrusted→MCP note).

## Consequences

- People author plugins as **standalone GitHub repos** and forks install them by
  URL with a **reproducible lock** + an **informed review gate**. Completes the
  extensibility arc: author (0018) → settings (0019) → surfaces (0026) →
  **distribute (0027)**.
- Safety is **informed trust + verifiable supply chain + audit**, not a sandbox —
  stated honestly; untrusted code routes to MCP.
- A future curated **index/registry** is a thin layer on top (it still installs via
  this path).

## Alternatives considered

- **Auto-enable on install** — rejected: install ≠ trust (D1).
- **Auto pip-install on install** — rejected: surprise code-exec (D4).
- **Per-plugin venv isolation** — impossible for in-process plugins (shared
  interpreter); real isolation = out-of-process = MCP.
- **Central registry/index first** — deferred: URL install is the primitive; an
  index is curation on top.
