# Plugins

Plugins are **drop-in packages** that extend protoAgent without forking it. A
plugin contributes **tools**, bundled **skills**, FastAPI **routes**, background
**surfaces**, **subagents**, **middleware**, knowledge backends/embedders, goal
verifiers — plus its own **config / secrets / Settings** (ADR 0018/0019/0032).
Plugins run **in-process** with the agent's privileges, so they're **disabled by
default** and you opt in explicitly — only enable plugins you trust.

> The first-party **Discord** and **Google** integrations ship as plugins
> (`plugins/discord/`, `plugins/google/`) — disable either with
> `plugins: { disabled: [discord] }` / `[google]`, no core edit. The opt-in
> **coding_agent** plugin (`plugins/coding_agent/`) adds `code_with` to spawn a
> CLI coding agent over ACP — see [Spawn CLI coding agents](/guides/coding-agents).

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
