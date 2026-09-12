# Install & publish plugins (git URLs)

Plugins can live in their own GitHub repo and be installed by URL — so you can
make one and share it, and pull others in. A plugin repo is a **complete package**: it
can contribute tools, subagents, SKILL.md skills, workflows, console views, routes,
MCP servers, and config — all from the one repo. See
[ADR 0027](/adr/0027-install-plugins-from-git-url) for the design + safety model.
(To install several plugins as one curated, pinned *set*, see
[Bundles](/guides/bundles) — a different thing than a single plugin repo.)

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
background surfaces come up live — no separate enable step and no restart. Its
declared settings are live too: the row's **Configure** dialog carries the plugin's
config fields immediately after install (both the Discover directory and
install-from-URL — no page refresh needed, #1643). The
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
gitignored (re-cloned from the lock). On a fresh checkout (or a restored data
dir) the console flags each locked-but-missing plugin and offers a one-click
**Sync plugins** button (`POST /api/plugins/sync`) — the same re-clone the CLI
does; plugins that are already in `plugins.enabled` come up live on the spot.

Upstream protoAgent ships the lock **empty** — a fresh clone starts with no
third-party plugins, by design. Your installs append to it; **forks and
deployments commit their lock** so their plugin set reproduces on every
checkout. (That means the upstream developer's own installs show as a local
diff on `plugins.lock` — expected; commit or discard as you see fit.)

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

## When a plugin moves into core (`supersedes`)

A standalone plugin can graduate into protoAgent's own `plugins/` tree. Keep its
**id** when it does: `plugins.enabled`, the plugin's config section and every
archetype's `enabled:` list are keyed by it, and a new id would orphan all three.
Then name the retired repo in the bundled manifest:

```yaml
# plugins/cowork/protoagent.plugin.yaml
id: cowork
name: Cowork
version: 0.4.0
supersedes:
  - https://github.com/protoLabsAI/cowork-plugin
```

On every host that upgrades, that one field changes the lifecycle above:

- **The bundled copy loads.** An installed copy that `plugins.lock` records as fetched
  from a listed URL stops shadowing it, at any version, and the operator gets a
  banner saying the old copy can be removed. Enabled state and settings carry over
  untouched, because the id didn't change.
- **Installs and archetypes keep working.** Installing from the old URL fetches
  nothing (the plugin already ships). An archetype bundle that still lists the member
  by URL treats it like `builtin: true`: skipped, not refused. So archetype repos need
  no change and keep working on older hosts too. (Bundles have no min-version, which
  is why they can't simply switch to `builtin: true`.)
- **Update stands down.** The freshness check reports the copy as `superseded`
  instead of *update available*, `POST /api/plugins/<id>/update` answers 409 with the
  reason, and the auto-update loop skips it with an info line instead of logging a
  failure on every sweep.
- **Uninstall removes only the leftover.** `plugin uninstall <id>` (or the console's
  Uninstall) deletes the ignored copy and its lock entry, and keeps the id in
  `plugins.enabled` along with its config section and secrets, even with `--purge`.
  They belong to the bundled copy now.

**A copy you placed by hand** — dropped in or symlinked into your plugins dir, with no
`plugins.lock` entry — is judged on version instead (that rule predates `supersedes`):
older than the bundled copy and it stops being what runs. That used to happen in silence;
now it says so, in the log and as a banner naming the path, and `plugin uninstall <id>`
removes exactly that path (a symlinked dev checkout is unlinked, never followed, so your
working tree survives). It never removes anything else: a folder named after the id that
holds a *different* plugin, a folder with no plugin in it, or the bundled tree itself are
all refused with the reason, and a symlink to a checkout that no longer exists is named
for you to remove rather than deleted. Keep such a copy in charge by giving it a version above the bundled one.

Three rules hold throughout. **A fork still wins**: a copy installed from any URL
*not* listed is a deliberate override, exactly as before. **Matching is exact about
the repo but not its spelling**: `https://`, `ssh://` and `git@host:` forms, letter
case, userinfo, port, a query or fragment, a leading `www.`, and a trailing `.git` or
`/` all compare equal, while globs, local paths and `file://` are rejected. And
**everything that acts on "the plugin" follows the copy that runs** — its description
and declared deps in the Plugins list, `install-deps`, the update check — so the
ignored copy can't send you after the wrong dependency list.

The move PR:

1. Vendor the plugin into `plugins/<id>/` — the folder named exactly for the manifest
   id (a guard test enforces that) — with `enabled: false` unless it should be on by
   default.
2. **Give the bundled copy a version above every release of the repo it supersedes.**
   That is a real requirement, not bookkeeping: if an installed copy ever loses its
   `plugins.lock` row (a hand-edited or reset lock), it becomes an untracked copy, and
   an untracked copy that isn't *older* than the bundled one wins (#1574). The loader
   warns while the copy is still recorded, so this shows up before it bites.
3. Add `supersedes:` with the retired repo's URL, and port its test suite into `tests/`.
4. Archive the old repo afterwards rather than deleting it: hosts that predate
   `supersedes` still clone it through archetype bundles.

## Keep a bundle fresh (the pin lifecycle)

A **bundle** (ADR 0040) pins each member so the combo it installs is the combo that
was verified together — but a pin that nothing re-verifies rots silently: the first
real bundle shipped pins that predated its members' console-view fixes, and every
agent spawned from the archetype got 404 panels. [ADR 0049](../adr/0049-bundle-pin-lifecycle.md)
gives the pin a lifecycle that keeps "last verified working" literally true:

1. **Pin release tags, not raw SHAs** (`ref: v0.1.1`) — legible, and the freshness
   check above can follow them (annotated tags compare by peeled commit).
2. **Record `verified_against:`** — the core version the pin set was last verified on.
3. **Let CI own the pin** — a verify job installs the manifest's pin set into a
   scratch agent and probes every declared console view on each PR + weekly, and a
   scheduled bump job opens a PR when a member tags a new release.

Start from the in-repo template — manifest, verify + bump scripts, and the GitHub
workflow, with the rules commented inline:
[`examples/bundles/template/`](https://github.com/protoLabsAI/protoAgent/tree/main/examples/bundles/template).

## Publish one

> **Start from the devkit.** Enable the bundled **`plugin-devkit`** plugin
> (`plugins: { enabled: [plugin-devkit] }`) — it's the canonical full-bundle
> example *and* it gives the agent the whole self-building loop (ADR 0096):
> `scaffold_plugin` (writes a skeleton **and enables it live**),
> `plugin_list_files` / `plugin_read_file` / `plugin_write_file` (inspect + edit it,
> fenced to the plugins dir), `test_plugin` (runs its pytest suite in a subprocess),
> `reload_plugins` (re-exec after an edit — a load failure reports with its
> traceback), `develop_plugin` (hand a substantial build to a configured `acp`
> coding delegate — it works scoped inside the plugin dir, then the host auto-runs
> test + reload), `register_plugin_project` (graduate a plugin to an ADR 0095
> managed project — fs tools, github picker, and a projectBoard can then target
> it), `enable_plugin`, `scaffold_bundle`, plus a `plugin-architect`
> subagent + `design-plugin` workflow + the `building-plugins` skill. Pass
> `git_init=True` (CLI: `--git`) to scaffold a repo from birth. With it on, ask the
> agent to *"build a plugin that …"* and it scaffolds, edits, tests, and hot-swaps it
> **in the same session — no restart**. Prefer the shell? `python -m server plugin
> new "My Plugin" --view --skill` (and `plugin new-bundle` for an ADR-0040 bundle)
> scaffold without the plugin enabled.

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
    registry.register_chat_command("issue", h)  # a user-only /<name> control command
    # skills/ and workflows/ are auto-discovered — no call needed. For a
    # non-standard location: registry.register_workflow_dir("recipes")
```

`register_chat_command(name, handler)` lets a plugin **own a `/<name>` chat
control command** — the generalized form of the core `/goal`. The handler is
`async (rest, session_id) -> str | None`: return a reply string to short-circuit
the turn (the model never runs), or `None` to pass the message through. It is
**user-only by design** — not an agent tool — so a plugin can expose a write
action (file an issue, open a PR) that the model can't trigger autonomously. Close
over `registry.config` to read your own settings. Precedence is `goal` >
`lifecycle` > plugin command > workflow > subagent > skill; `goal` and `lifecycle`
are reserved core tokens (a plugin can't claim either).

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

### Dep scope: which interpreter has to import it

On the desktop app there are **two** Pythons with separate site-packages: the frozen
host process, and the managed Python runtime that serves `execute_code` children. A
`requires_pip` entry can say which one needs the dep:

```yaml
requires_pip:
  - "httpx>=0.27"                          # runtime-scoped (the default)
  - { pkg: "pillow>=10", optional: true }  # optional tier
  - { pkg: "numpy", scope: host }          # imported IN-PROCESS by the plugin's tools
```

`scope: runtime` (the default, matching the compute-plugin pattern) means the managed
runtime satisfies it and the wheel installer provisions it. **`scope: host` means your
plugin's own module imports it** — the managed runtime can never satisfy that, so on a
frozen host the install gate refuses honestly instead of passing and letting every tool
call die with `ModuleNotFoundError` (#2246). If you hit that refusal, vendor the code,
drop the dependency, or ship it in the app bundle.

An unrecognized `scope` warns and falls back to the default rather than rejecting the
plugin.

`min_protoagent_version` is enforced at load: the plugin is refused when it needs a
newer host. The host version it's compared against is the same shared resolver the
A2A agent card advertises (`infra.paths.package_version()` — the repo
`pyproject.toml` on a source checkout, installed package metadata on wheel/frozen
installs), so the gate and the card always agree — a dev checkout's stale
editable-install metadata can no longer refuse a valid plugin (#1644).

## Get listed in the directory

Anyone can install your plugin from its git URL once it's a public repo. To make it
**discoverable** and feature it on the [plugin directory](https://agent.protolabs.studio/plugins):

1. **Tag the repo** with the [`protoagent-plugin`](https://github.com/topics/protoagent-plugin)
   GitHub topic — that surfaces it in the topic search across GitHub, and the plugin
   directory auto-discovers it on the next site deploy (bundles also tag
   `protoagent-bundle` to group under Bundles).
2. **Open a PR** adding an entry to
   [`config/plugin-directory.yaml`](https://github.com/protoLabsAI/protoAgent/blob/main/config/plugin-directory.yaml)
   and run `python scripts/plugin_directory.py build`. That one entry drives every
   curated surface: the site card's polish (`name`, `tagline`, `adds`) **and** the
   in-app Plugins ▸ Discover catalog (`GET /api/plugins/catalog`). The derived files
   (`config/plugin-catalog.json`, `sites/marketing/data/plugins.json`) are generated —
   don't edit them by hand; CI fails on drift.
3. **Pick a `status`** (default `active`). It decides where the plugin is listed:

   | status | in-app Discover | website directory |
   |---|---|---|
   | `active` | listed | listed |
   | `incubating` | — | listed, with an *incubating* badge |
   | `personal`, `archived` | — | hidden |
   | `deprecated`, `internal` (older values, still accepted) | — | hidden |

   The directory is the full census of the org's plugin repos, so a plugin that's
   built for one setup or no longer maintained keeps its row with an honest status
   rather than being removed. A hidden row also drops the card the site would
   otherwise auto-discover from the repo's topic.

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
