# Plugins

Plugins are **drop-in packages** that extend protoAgent without forking it. A
plugin contributes **tools**, bundled **skills**, FastAPI **routes**, background
**surfaces**, **subagents**, **middleware**, knowledge backends/embedders, goal
verifiers — plus its own **config / secrets / Settings** (ADR 0018/0019/0032).
Plugins run **in-process** with the agent's privileges, so they're **disabled by
default** and you opt in explicitly — only enable plugins you trust.

> The first-party **Discord**, **Google**, and **GitHub** integrations ship as
> plugins (`plugins/discord/`, `plugins/google/`, `plugins/github/`) — Discord/Google
> are on by default (disable with `plugins: { disabled: [discord] }`); GitHub is
> opt-in (`plugins: { enabled: [github] }`). To drive a **CLI coding agent over
> ACP**, enable the **delegates** plugin and declare an `acp` delegate — see
> [CLI coding agents over ACP](/guides/coding-agents).

> **Trust model.** This is the in-process / trusted model (matching Hermes): an
> enabled plugin's `register()` runs as the agent. Don't enable code you
> haven't reviewed. Untrusted third-party *tools* are better added via
> [MCP](./mcp.md) (out-of-process).

## Anatomy

A plugin is a directory with a manifest and a module exposing `register(registry)`:

```
plugins/hello/
├── protoagent.plugin.yaml   # manifest
├── __init__.py              # def register(registry): ...
└── skills/                  # optional bundled SKILL.md skills
    └── greeting/SKILL.md
```

### Manifest — `protoagent.plugin.yaml`

```yaml
id: hello                 # required, unique
name: Hello Plugin        # required
version: 0.1.0
description: One-line summary.
enabled: false            # author opt-in; operators can also enable by id in config
requires_env: []          # env vars the plugin needs (missing → skipped + logged)
capabilities:             # declarative, for transparency (not yet enforced)
  network: []
  filesystem: none
emits: []                 # event-bus topics this plugin broadcasts (ADR 0039) — its public API
subscribes: []            # topics it listens for (declarative — for discoverability)
```

### Entry — `register(registry)`

```python
from langchain_core.tools import tool

@tool
async def hello(name: str = "world") -> str:
    """Return a friendly greeting."""
    return f"Hello, {name}!"

def register(registry):
    registry.register_tool(hello)        # expose a LangChain tool
    registry.register_skill_dir("skills")  # bundle SKILL.md skills (relative to the plugin)
```

`register` is called once at load. The registry accepts these contribution types
(plus console **views**, declared in the manifest — see [Plugin console views](/guides/plugin-views)) —
a fork adds any of them as a plugin, never editing the core `server/` package:

| Method | Contributes | Lifecycle |
|---|---|---|
| `register_tool(tool)` / `register_tools(iter)` | A LangChain tool | graph build (live-reloads) |
| `emit(topic, data)` / `on(topic, handler)` | Broadcast / subscribe on the **event bus** (ADR 0039) — `emit` auto-namespaces to `<plugin>.<topic>`; `on` takes `*`/`#` wildcards | any time (publish is fire-and-forget) |
| `register_skill_dir(path)` | A `SKILL.md` directory (procedural memory) | graph build |
| `register_workflow_dir(path)` | A directory of `*.yaml` workflow recipes | workflow-registry build |
| `register_a2a_skill(spec)` | An A2A **card** skill (what the card advertises; optional structured output) | agent-card build |
| `register_router(router, prefix=None)` | A FastAPI `APIRouter` | **mounted once** at init (default prefix `/plugins/<id>`) |
| `register_surface(start, stop=None, name=None, reload=None)` | A background surface (a Discord-style gateway) | `start` in startup, `stop` in shutdown, `reload(cfg)` on config save |
| `register_subagent(config)` | A `SubagentConfig` (a delegate) | added to `SUBAGENT_REGISTRY` |
| `register_middleware(factory)` | A LangGraph **`AgentMiddleware`** (per-turn before/after-model + tool hooks) — `factory(config) → middleware \| None` | graph build; appended before message-capture (ADR 0032) |
| `register_mcp_server(factory)` | A **managed MCP server** the agent connects to | `factory(config)` called at each graph build → entry dict or `None` |
| `register_thread_id_resolver(fn)` | A `(request_metadata, session_id) → str` checkpointer-scope resolver (e.g. per-project memory) | each turn; one wins (last plugin) |

```python
def register(registry):
    registry.register_tool(hello)
    registry.register_a2a_skill({"id": "greet", "name": "Greet", "description": "..."})
    registry.register_router(_build_router())        # → GET /plugins/<id>/...
    registry.register_surface(_start, stop=_stop, name="my-surface")
    registry.register_subagent(_build_subagent())    # delegate via task/task_batch
    registry.register_mcp_server(_server_factory)    # a managed MCP server (e.g. Google)
    registry.register_thread_id_resolver(lambda md, sid: f"proj:{md.get('project')}:{sid}")
```

### Managed MCP servers — `register_mcp_server`

A plugin can ship a **managed MCP server** the agent connects to, instead of
making the operator hand-edit `mcp.servers`. The factory is called at every graph
build with the live `LangGraphConfig`; return a `mcp.servers[]` entry (`{name,
transport, command, args, env, ...}`) when the server should run, or `None` when
it shouldn't (off / not yet connected) — so the server comes and goes with config.
A returned entry whose `name` matches a configured server replaces it, and a
factory that returns an entry activates MCP even when `mcp.enabled` is off. This
is how the first-party **Google** plugin ships its OAuth-gated Gmail/Calendar
server (`plugins/google/`). For a frozen desktop build (no `python` on PATH),
launch via `args: ["--mcp-plugin", "<id>"]` and expose a `mcp_main()` in your
plugin module — the binary re-invokes itself and the shim runs it.

### Middleware — `register_middleware` (ADR 0032)

A plugin can contribute a LangGraph **`AgentMiddleware`** — the per-turn hook layer
(`before_model` / `after_model` / `wrap_tool_call` / …) the core uses for knowledge
injection, enforcement, compaction, and audit. The factory gets the live config and
returns a middleware instance (or `None` to opt out); it's appended to the chain just
before the internal message-capture middleware, so its hooks run and the turn is still
captured.

For **per-request** data (the A2A request's merged metadata — project scope, origin,
caller keys), read `current_request_metadata()` — a contextvar bound for the duration
of each turn. This is how a fork injects a per-turn directive without editing the core
executor:

```python
from langchain.agents.middleware import AgentMiddleware
from graph.middleware.request_context import current_request_metadata

class ScopeBannerMiddleware(AgentMiddleware):
    def before_model(self, state, runtime):
        project = current_request_metadata().get("project")
        if not project:
            return None
        banner = SystemMessage(content=f"Active project scope: {project}. Stay within it.")
        return {"messages": [banner, *state["messages"]]}

def register(registry):
    registry.register_middleware(lambda config: ScopeBannerMiddleware())
```

## Host services — `registry.host`

A surface or route often needs to **call the agent** or the **event bus** — host
services it can't build. `registry.host` exposes them (the server populates them
before any surface starts; guard for `None`):

- `host.invoke(prompt, session_id)` — run a chat turn (one conversation per
  `session_id`), returns the assistant text.
- `host.publish(event, data)` / `host.subscribe()` — the server→client event bus.
- `host.on(topic, handler)` — subscribe an in-process handler to bus topics (ADR 0039); prefer the
  `registry.emit` / `registry.on` wrappers, which namespace + guard for you.
- `host.config()` — the live `LangGraphConfig` (current resolved values, incl.
  `plugin_config`), so a route reads fresh config instead of a load-time snapshot.
- `host.apply_settings(patch)` — persist a nested config patch + reload once
  (heavy — call via `asyncio.to_thread`). Lets a route apply config (e.g. Google's
  Connect flow flips `enabled` and reloads).

```python
def register(registry):
    host = registry.host
    async def _on_message(text, sid):
        return await host.invoke(text, sid)        # call the agent
    registry.register_surface(lambda: _gateway(_on_message), name="my-gateway")
```

### Tapping core deeper — `graph.sdk` (ADR 0043)

`registry.host` covers the common cases. For deeper capability, import the **consumption
SDK** directly — `from graph.sdk import …`, the *stable* surface plugins call into core
(so core can refactor underneath you; never reach into `graph.agent` internals). v1:

- `run_subagent(subagent_type, prompt, *, description)` — run **one subagent** to
  completion (vs `host.invoke`, which runs a full lead-agent *chat turn*).
- `subagent_types()` — the configured subagent ids.
- `config()` — the live `LangGraphConfig`.

The **workflows plugin** (`plugins/workflows`) is the reference consumer: its engine
injects `run_subagent` as the per-step runner. This is the pattern for plugins that tap
core, not just contribute to it.

## Events — the plugin bus (ADR 0039)

Plugins coordinate by **broadcasting events**, never by importing each other. You publish under your
own namespace and forget; anyone who cares subscribes by topic. This is the only inter-plugin
channel — the **no-cross-dependency** rule.

```python
def register(registry):
    registry.emit("created", {"id": "a1"})    # → publishes "<plugin_id>.created"
    registry.on("notes.*", on_notes)          # subscribe to ANY topic; * / # wildcards
```

- **Publish is namespace-guarded** — `emit("created")` becomes `<plugin_id>.created`; you can only
  publish under your own namespace. **Subscribing is read-only** and may match any topic.
- **Declare your contract** in the manifest (`emits:` / `subscribes:`) — your events are your public
  API, discoverable in `/api/runtime/status`.
- A console **view** (sandboxed iframe) talks to the bus over the bridge — see
  [Plugin console views](/guides/plugin-views). Any event under `<plugin_id>.*` lights your plugin's
  rail icon (a **notification dot**) until the user opens that surface.
- Fire-and-forget + topic-filtered + exception-isolated: a slow or broken subscriber can't affect the
  publisher or other subscribers. Ephemeral (a ring buffer covers SSE reconnects; no durable log).

> Cross-process note: under the **ACP runtime**, a tool runs in the operator-MCP process where the
> bus isn't wired, so `emit` from a tool won't reach the server bus there. Under the default runtime
> (tool runs in-server) it does.

## Performance — keep the burden in your plugin

The core console is deliberately lean: one push-based SSE connection, no always-on polling (its
react-query refetches pause when the window is backgrounded). A plugin should be just as
well-behaved — the *only* extra cost should be the one your plugin chooses to add, and it should
go quiet when nobody's looking. This matters doubly for the desktop build.

- **Prefer events over polling.** Subscribe to the bus (`registry.on` / `protoagent:event`) instead
  of polling an endpoint on a timer where you can.
- **If you must poll, pause when hidden.** In a served view, guard the loop with the Page Visibility
  API and refresh on return — don't poll a minimized window:
  ```js
  setInterval(() => { if (!document.hidden) refresh(); }, 1500);
  document.addEventListener("visibilitychange", () => { if (!document.hidden) refresh(); });
  ```
- **Clean up on unmount.** The console unmounts a plugin view's iframe the moment you tab/collapse
  away — your in-iframe timers/listeners die with it for free. For host-side work (a `registry.on`
  handler, a background surface), return/register a teardown so nothing lingers.

## Config, secrets & settings (ADR 0019)

A configurable plugin **declares its config in the manifest** (data, so it's known
at config-load time before `register()` imports). It claims a top-level config
section (default: the plugin id) and gets a Settings group + secrets routing —
no `config.py` / `settings_schema.py` edit:

```yaml
# protoagent.plugin.yaml
config_section: hello          # top-level YAML section (default: the id)
config: { greeting: "Hello", api_key: "" }   # defaults
secrets: [api_key]             # → secrets.yaml (redacted in the UI)
settings:                      # System → Settings group (named after the section)
  - { key: greeting, label: "Greeting word", type: string }
  - { key: api_key,  label: "API key",       type: secret }
```

Read the resolved config (manifest defaults ⊕ YAML ⊕ secrets) in `register()`:

```python
def register(registry):
    greeting = registry.config.get("greeting", "Hello")  # ADR 0019
    registry.register_router(_build_router(greeting))    # close over it
```

A plugin section colliding with a reserved built-in (`model`, `mcp`, `plugins`,
…) is ignored. (`discord` and `google` are **not** reserved — they're claimed by
the first-party Discord/Google plugins.)
The **wizard step** is not yet plugin-contributable (Settings + a docs link
suffice for now).

**Routes + surfaces are wired once at process init and don't hot-reload** — a
config reload reuses them, so changing `plugins.enabled` needs a restart
(ADR 0018). Everything is best-effort: a failing plugin/route/surface logs and
never breaks boot. The shipped [`plugins/hello`](https://github.com/protoLabsAI/protoAgent/tree/main/plugins/hello)
example demonstrates the contribution types. Plugin contributions show in
`GET /api/runtime/status`. The `plugins/discord` and `plugins/google` first-party
plugins are worked examples of a surface + route and a managed MCP server + route.

## Where plugins live & how they're enabled

Two roots (like skills): bundled `plugins/` (shipped, e.g. the `hello` example)
and live `<config-dir>/plugins/` (your drop-ins; `<config-dir>` honors
`PROTOAGENT_CONFIG_DIR`, override with `plugins.dir`). Live overrides bundled by `id`.

A plugin loads only when **enabled** — either:

```yaml
plugins:
  enabled: [hello]   # operator opt-in, by id
```

or `enabled: true` in the plugin's own manifest (author opt-in for plugins you
wrote/dropped in). Discovered-but-disabled plugins still appear in runtime
status so you can see what's available.

From the console, the **Plugins** panel has a one-click **Enable / Disable** toggle per
plugin — it edits `plugins.enabled` and hot-reloads, so tools / middleware / MCP servers
apply immediately. A plugin that serves a **console view** or runs a **background surface**
(its router mounts at startup) needs a restart to finish — the toggle says so.

Plugin tools that would shadow a core or MCP tool name are skipped (logged).
Bundled skills load as `disk`-source [skills](./skills.md), re-seeded each boot.

## Behavior

- Loading is **best-effort**: a broken plugin (bad manifest, import error,
  missing `requires_env`) is logged and skipped — it never blocks boot.
- `GET /api/runtime/status` lists `plugins` with `{id, name, enabled, loaded,
  tools, skills}`.
- Plugins are (re)loaded at startup and on config reload.

## Try it

Enable the shipped example:

```yaml
plugins:
  enabled: [hello]
```

Restart, then check `GET /api/runtime/status` — the `hello` plugin shows
`loaded: true` with its `hello` tool and `greeting` skill.

## Related

- **[Plugin console views](/guides/plugin-views)** — give a plugin its own
  left-rail icon + view (a dashboard) in the console (ADR 0026).
- **[Install & publish plugins (git URLs)](/guides/plugin-registry)** — install a
  plugin from a git URL (`python -m server plugin install <url>`) or publish one as
  a shareable repo. A repo is a full bundle: besides what `register()` adds, a
  conventional `skills/` (SKILL.md) and `workflows/` (`*.yaml`) are auto-discovered
  (ADR 0027).
