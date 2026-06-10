# Install & publish plugins (git URLs)

Plugins can live in their own GitHub repo and be installed by URL — so you can
make one and share it, and pull others in. A plugin repo is a **full bundle**: it
can contribute tools, subagents, SKILL.md skills, workflows, console views, routes,
MCP servers, and config — all from the one repo. See
[ADR 0027](/adr/0027-install-plugins-from-git-url) for the design + safety model.

## Install one

**CLI:**
```sh
python -m server plugin install https://github.com/owner/protoagent-plugin-x --ref v1.0
python -m server plugin list
python -m server plugin uninstall protoagent-plugin-x            # code + lock + enabled ref
python -m server plugin uninstall protoagent-plugin-x --purge    # also config section + secrets
python -m server plugin sync          # re-clone the locked set (CI / fresh checkout)
python -m server plugin install-deps protoagent-plugin-x   # explicit, separate
```

**Uninstall removes** the plugin's code, its `plugins.lock` entry, and its
`plugins.enabled` reference (so nothing dangles). It **keeps** the plugin's config
section + secrets by default (a reinstall restores your settings); pass `--purge`
to remove those too. Declared pip deps are **never** auto-removed (shared venv) —
they're reported so you can `pip uninstall` them if unused.

**Console:** the **Plugins** section → **Download** — paste the URL, review the
manifest + capabilities, install, uninstall.

**Installing from the console AUTO-ENABLES + runs the plugin** (trust-by-default):
it's added to `plugins.enabled` and hot-reloaded, so its tools, console views and
background surfaces come up live — no separate enable step and no restart. The
console flashes a one-time "this runs code on your machine" confirm for unofficial
sources first (official `protoLabsAI/*` installs skip it; "don't show again" flips to
full trust). Only install code you trust — for untrusted code, use an MCP server.

> The **CLI** `plugin install` stays fetch-only by design (install ≠ enable) for
> reproducible/scripted setups — enable explicitly via `plugins.enabled`. Set
> `PROTOAGENT_PLUGIN_INSTALL_NO_ENABLE=1` to make the console behave the same way.

```yaml
plugins:
  enabled: [protoagent-plugin-x]   # the console auto-adds this for you on install
```

Install pins the **resolved commit SHA** and records it in a committed
`plugins.lock`, so `plugin sync` reproduces the exact set. The code itself is
gitignored (re-cloned from the lock).

## Keep one up to date

Because the lock pins a commit SHA, an installed plugin doesn't move until you
update it. The console surfaces this for you: the **Plugins** rail (Local tab)
and **Settings → Integrations** show a freshness badge next to each plugin's
version —

- **up to date** — the locked SHA matches the latest commit on its ref
- **update available** — the remote ref has moved ahead → an **Update** button appears
- **pinned** — the plugin was installed at a specific commit SHA (`--ref <sha>`),
  so it intentionally never auto-updates (update it by reinstalling at a new ref)
- **check failed** — the remote couldn't be reached (the row still works)

Clicking **Update** pulls the latest code at the plugin's recorded ref, rewrites
the lock with the new SHA, and — if the plugin is enabled — hot-reloads it in
place. A plugin that contributes a **console view or background surface** can't
swap its already-mounted router live, so updating it recommends a restart to
finish loading the new view (the UI tells you when).

The freshness check runs `git ls-remote` against the recorded `source_url` and is
timeout-bounded + briefly cached, so it never hangs the panel. Pinned plugins skip
the network entirely.

## Publish one

> **Start from the devkit.** Enable the bundled **`plugin-devkit`** plugin
> (`plugins: { enabled: [plugin-devkit] }`) — it's the canonical full-bundle
> example *and* it gives the agent a `scaffold_plugin` tool, a `plugin-architect`
> subagent + `design-plugin` workflow, and the `building-plugins` skill. With it
> on, just ask the agent to *"build a plugin that …"* and it scaffolds a working
> skeleton for you to fill in.

A plugin is a directory (its own repo) with a manifest + a `register()`. The
**conventional layout** — everything here is picked up when the plugin is enabled:

```
my-plugin/
  protoagent.plugin.yaml      # manifest (id, name, version, requires_pip, views, …)
  __init__.py                 # def register(registry): … — tools, subagents, etc.
  skills/                     # SKILL.md skills — auto-discovered (data, no code)
    my-skill/SKILL.md
  workflows/                  # *.yaml workflow recipes — auto-discovered (data)
    my-recipe.yaml
```

`register(registry)` contributes the **code** extensions:

```python
def register(registry):
    registry.register_tool(my_tool)            # a LangChain tool
    registry.register_subagent(my_subagent)    # a SubagentConfig
    registry.register_router(my_router)         # FastAPI routes at /plugins/<id>
    registry.register_mcp_server(my_factory)    # a managed MCP server
    # skills/ and workflows/ are auto-discovered — no call needed. For a
    # non-standard location: registry.register_workflow_dir("recipes")
```

`skills/` and `workflows/` are **data**, so they're auto-discovered from those
conventional subdirs — no boilerplate. **Console views** (a rail icon + page) are
declared in the manifest — see [Building a plugin view](/guides/building-react-plugin-views).

Declare pip dependencies (they are **not** auto-installed — see Safety):

```yaml
# protoagent.plugin.yaml
id: my-plugin
name: My Plugin
version: 1.0.0
repository: https://github.com/owner/my-plugin
requires_pip: ["httpx>=0.27"]
min_protoagent_version: "0.20.0"
```

## Get listed in the directory

Anyone can install your plugin from its git URL once it's a public repo. To make it
**discoverable** and feature it on the [plugin directory](https://agent.protolabs.studio/plugins):

1. **Tag the repo** with the [`protoagent-plugin`](https://github.com/topics/protoagent-plugin)
   GitHub topic — that surfaces it in the topic search across GitHub.
2. **Open a PR** adding an entry to
   [`sites/marketing/data/plugins.json`](https://github.com/protoLabsAI/protoAgent/blob/main/sites/marketing/data/plugins.json)
   (`name`, `tagline`, `adds`, `install` = your git URL, `links`). It renders as a card on the directory.

## Safety

The model is **informed trust + a verifiable supply chain**, not a sandbox — an
enabled plugin runs in-process *as the agent* (like a pip dependency). So:

- **Install ≠ enable ≠ trust.** Installing only fetches code + reads the manifest
  (data); it never imports the plugin. Enabling (`plugins.enabled`) is the trust
  decision — review the manifest + capabilities first.
- **Deps are explicit.** `requires_pip` is declared, never auto-installed (pip runs
  arbitrary build code). Run `plugin install-deps <id>` after reviewing them; a
  missing dep gives a clear "run install-deps" message on enable.
- **Pinned + reproducible.** Installs pin a commit SHA in `plugins.lock`.
- **Optional source allowlist.** Lock installs down to trusted orgs:
  ```yaml
  plugins:
    sources:
      allow: ["github.com/yourorg/*"]
  ```
- **Audited.** install / uninstall / install-deps are written to the audit log.
- **Untrusted code? Use [MCP](/guides/mcp) instead** — it runs out-of-process and
  is sandboxable. Git plugins are for code you've reviewed and trust.
