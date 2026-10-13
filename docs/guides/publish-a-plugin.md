# Publish a plugin

Publish a plugin that already works locally, declare its dependencies, and make
it available to other users. Start with [Build your first plugin](/tutorials/first-plugin)
if you still need a scaffold. For installation and updates in the app, use
[Install and manage plugins](/guides/plugin-registry).

## Publish one

Use `protoagent plugin new "My Plugin" --view --tests --git` to create a
scaffold with a test suite and local Git history. In a source checkout, replace
`protoagent` with `uv run python -m server`. The bundled `plugin-devkit` also lets
the agent scaffold, test, and reload a plugin; see [Build plugins](/guides/plugins).

Before publishing, run the plugin's tests, enable it on a compatible host, and
try its tools and views. Push the repository and tag a version so users can
install that release by URL and ref.

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
frozen host the install gate refuses the install instead of allowing a later `ModuleNotFoundError`. If you hit that refusal, vendor the code,
drop the dependency, or ship it in the app bundle.

An unrecognized `scope` warns and falls back to the default rather than rejecting the
plugin.

### Platform-specific deps (environment markers)

A `requires_pip` entry may carry a [PEP 508 environment marker](https://peps.python.org/pep-0508/#environment-markers)
after a `;` — the same syntax pip accepts:

```yaml
requires_pip:
  - "pywinpty>=2.0; sys_platform == 'win32'"      # Windows only (the Terminal plugin)
  - "tomli>=2; python_version < '3.11'"            # only on older Pythons
```

A dep whose marker is **false on this machine isn't required here**: it never shows as
missing (the banner, the Plugins row, the install dialog, the CLI), and Install deps never
hands it to pip. So the Terminal plugin needs nothing on macOS or Linux (it uses the stdlib
`pty`), and asks for `pywinpty` only on Windows. A spec that can't be parsed is checked by
its package name, as before, and pip gets the final say on it.

### Installing a plugin's deps from the console

Install never runs pip (see Safety). When the plugin you just installed — from Discover or
from a git URL — declares packages this machine is missing, the console asks **once**:
*"&lt;Plugin&gt; needs these Python packages — install them now?"*. The dialog lists the
**exact specs** pip will be handed (only the ones that apply to this platform, with the
optional tier marked best-effort), the plugin's **source**, and **where** they install
(this server's Python environment, or the desktop app's managed Python runtime). Confirming
installs them in the same flow and shows the result — pip's error summary on failure, with
Retry. **Not now** leaves the plugin installed; it then shows the warning banner below. A
plugin missing only optional packages doesn't prompt — the install toast names them.

**The desktop app asks the same way.** Its install no longer pips a missing required dep
into the managed Python runtime on its own (it did, #2226): the plugin lands, and the same
dialog lists the packages with *where* reading "the desktop app's managed Python runtime".
Confirming installs them there through the same route; if the runtime isn't provisioned yet,
the error names Settings ▸ Tools. One refusal stays: a **required `scope: host` dep** the app
doesn't bundle still refuses the install — the plugin imports it in the app's own process,
which the managed runtime can't serve, so no confirm could fix it (see *Dep scope* above).
Unattended installs that have no dialog to show — creating a fleet agent from an archetype
and importing an agent snapshot — pass the explicit opt-in `plugin install
--install-runtime-deps`, which keeps installing required deps into the managed runtime as
part of the install (no effect on a server, where install never pips). Plugin updates and
auto-updates don't ask: a dep a new version adds shows up as the warning banner below.

An enabled plugin whose required packages are missing raises a warning banner
(*"can't run until its Python packages are installed: …"*) with an **Install dependencies**
button: it installs them right there, shows progress on the button, reports the result as a
toast, and the banner clears once they land — no trip to Settings. It, the install dialog
and the Plugins row's **Install deps** all call the same route,
`POST /api/plugins/install-deps` (source-trust re-check, one install at a time), and the CLI
equivalent is `plugin install-deps <id>`.

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
   built for one setup or no longer maintained keeps its row with the appropriate status
   rather than being removed. A hidden row also drops the card the site would
   otherwise auto-discover from the repo's topic.

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
all refused with the reason. A link at `<plugins dir>/<id>` whose checkout no longer
exists is unlinked (it has no target, so nothing else can be touched) and named in the
output. The same check guards every `plugin uninstall`, bundled id or not: with
`plugins.dir` pointed at a folder of checkouts, a folder is removed only if it holds that
plugin, or `plugins.lock` records installing it there. Links, including Windows directory
junctions, are unlinked and never followed. Keep such a copy in charge by giving it a version above the bundled one.

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
was verified together — but verify those pins when core or members change. [ADR 0049](../adr/0049-bundle-pin-lifecycle.md)
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

## Safety

The model is **informed trust + a verifiable supply chain**, not a sandbox — an
enabled plugin runs in-process *as the agent* (like a pip dependency). So:

- **Review before activation.** CLI installation fetches code without enabling
  it. Console installation enables and runs it immediately; review the source
  and capabilities before installing there.
- **Deps are explicit.** `requires_pip` is declared, never auto-installed (pip runs
  arbitrary build code). Run `plugin install-deps <id>` after reviewing them, or confirm
  the console's install-time dialog, which lists the exact specs and the plugin's source
  before anything runs; a missing dep gives a clear "run install-deps" message and an
  Install dependencies banner on enable.
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
