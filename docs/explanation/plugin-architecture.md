# Plugin architecture

protoAgent is a **template repo**: shared-runtime work lands upstream, and your agent's personality
and domain logic live in a fork. The plugin system exists so that *capability* doesn't have to work
that way. A plugin adds tools, routes, console views, subagents, middleware, and background work to
a running agent without editing core — so upstream stays mergeable and your extension stays yours.

This page explains the model. For the exact surfaces, see the
[registry API](/reference/plugin-registry-api), [SDK](/reference/plugin-sdk-api), and
[manifest](/reference/plugin-manifest) references; the decisions are recorded in
[ADR 0001](/adr/0001-extensibility-and-plugin-architecture) and its successors.

## A plugin is a directory the host reads before it runs

Two things define a plugin: a `protoagent.plugin.yaml` manifest and a `register(registry)` function.

The split matters more than it looks. **The manifest is parsed before any of your Python is
imported.** That is what lets the console list a plugin it has never run, show its settings, route
its config and secrets, warn about its dependencies, and let an operator review an install before
consenting to it. If enablement had to run code to discover what a plugin wanted, "review before you
enable" could not exist.

So anything the host must know *about* a plugin without executing it is declarative — the views it
contributes, the config section it claims, the env it requires, the topics it emits, the paths it
wants exempted from auth. Anything it *does* is Python.

## Two planes, two trust levels

Plugins extend protoAgent on two very different planes, and conflating them is the most common
source of confusion.

**The Python plane runs in-process, as the agent.** `register()` executes inside the server with the
agent's full privileges — its filesystem access, its credentials, its network. There is no sandbox.
This is a deliberate trade (the same one Hermes makes): in-process means a plugin can contribute a
real LangChain tool, wrap the model call in middleware, or hold a websocket open, none of which a
sandbox would allow. The cost is that **enabling a plugin is a trust decision**, which is why
plugins are disabled by default and enablement is always explicit. For untrusted third-party tools,
[MCP](/guides/mcp) is the out-of-process alternative — that's what it's for.

**The console plane runs sandboxed.** A plugin's view is an iframe, and the only channel to the
console is `postMessage`. It cannot read the operator's session, reach into console state, or call
a host API it wasn't handed a token for. The
[view bridge](/reference/plugin-view-bridge) is that channel's entire vocabulary.

The asymmetry is intentional: your *backend* is trusted because it must be, your *frontend* is
sandboxed because it needn't be.

## The load lifecycle

Understanding when your code runs explains most "why didn't that take effect" questions.

1. **Discover.** Two roots are scanned — bundled (`<repo>/plugins/`) and live
   (`<instance>/plugins/`, or `plugins.dir`).
2. **Parse the manifest.** No import yet.
3. **Gate.** A plugin loads only if `enabled: true` (author opt-in) or its id is in
   `plugins.enabled` (operator opt-in), and it isn't in `plugins.disabled`. A `builtin` plugin is
   core runtime infrastructure and ignores both lists. Then `requires_env` must be satisfied and
   `min_protoagent_version` must be met, or the plugin is skipped with a logged reason.
4. **Import the entrypoint** (`__init__.py`, then `plugin.py`, or the declared `entrypoint`) under a
   synthetic package name, so relative imports inside the plugin work.
5. **Call `register(registry)`.** Everything you contribute is recorded on the registry — nothing is
   wired yet.
6. **Build the graph.** Tools, middleware, subagents, knowledge backends, and late tool factories are
   composed into the agent here.
7. **Start the server.** Routers mount; background surfaces `start()`.
8. **Reload** re-runs this without a restart. Surfaces get `reload(config)` if they declared one.

Two consequences worth internalizing. First, **`register()` is not a good place to do work** — it
runs during boot, on the import path, and blocking there delays the whole agent; register a surface
and do the work in its `start()`. Second, **module-level state does not survive a reload**, which is
why a background surface that should resume its work must persist the operator's *intent* on disk
rather than assuming it kept running.

## Everything a plugin owns is namespaced by its id

The same principle recurs across every seam, and it's the system's main defense against one plugin
reaching into another:

| What | Namespace |
|---|---|
| Routes | `/plugins/<id>/…` (pages) and `/api/plugins/<id>/…` (data) |
| Config + secrets | the `config_section` (defaults to the id) |
| Event topics published from a view | force-rewritten to `<id>.<topic>` |
| Scheduled jobs | `plugin:<id>:<job_id>`, so the host can sweep them when the plugin is disabled |
| Metric series | `<plugin_id>:<name>` |
| Keybinding + context-menu ids | `plugin.<id>.<local id>` |
| Auth-exempt paths | rejected unless under the plugin's own prefix |

Note that several of these are *forced* rather than *checked* — a view asking to publish `chat.new`
gets `<its-id>.new`. That's the difference between a convention and a boundary.

## Plugins never import each other

The event bus ([ADR 0039](/adr/0039-plugin-event-bus)) is the only inter-plugin channel. A plugin
publishes topics as its public API and others subscribe by name, so no plugin has a hard dependency
on another being installed, enabled, or a particular version. A plugin can declare its topics — and
optionally their payload schemas — in the manifest, which is how a consumer discovers a contract
instead of reverse-engineering the emitter.

The same bus reaches sandboxed views through the relay, so a console surface can react to backend
events without polling.

## What's stable, and what isn't

Three surfaces are the contract, and are kept compatible:

- **`registry`** — the `register_*` seams ([reference](/reference/plugin-registry-api))
- **`registry.host`** — invoke the agent, the event bus, live config
- **`graph.sdk`** — capability without internals: subagents, knowledge, watches, background jobs,
  scheduling, metrics ([reference](/reference/plugin-sdk-api), [ADR 0043](/adr/0043-plugin-consumption-sdk-workflows-extraction))

Everything else in `graph/`, `server/`, and the infra packages is **internal** and changes between
releases. A plugin that imports `graph.agent` internals or reaches into server state is depending on
something nobody promised to keep — and the failure mode is quiet, because a lazy import inside a
function only breaks when that function is called. Guard host imports you can't avoid, and prefer
the SDK where it covers the need.

## Where state lives

Config and secrets are **per instance**, always — one agent's plugin config never leaks into
another's ([ADR 0004](/adr/0004-multi-instance-data-scoping),
[ADR 0065](/adr/0065-two-tier-instance-paths)). Two agents share a *capability* by installing the
same plugin or bundle, never by sharing a mutable store.

Data follows the tiered model: private per instance by default, with an opt-in shared commons for
things that genuinely benefit from being shared ([ADR 0041](/adr/0041-workspaces-and-tiered-stores)).
A plugin that needs its own persistence gets a directory under the instance's store — and should not
open core's databases, which carry no compatibility promise for outside readers.

## Related

- [Plugins guide](/guides/plugins) — how to build one
- [Build your first plugin](/tutorials/first-plugin) — the twenty-minute version
- [Install & publish plugins](/guides/plugin-registry) — distribution, pinning, bundles
- [ADR 0018](/adr/0018-plugin-surfaces-routes-subagents) · [ADR 0019](/adr/0019-plugin-config-settings-secrets) · [ADR 0026](/adr/0026-plugin-contributed-console-surfaces) · [ADR 0027](/adr/0027-install-plugins-from-git-url) · [ADR 0071](/adr/0071-plugin-permissions-trust-model)
