# Plugins

Plugins are **drop-in packages** that extend protoAgent without forking it. A
plugin contributes **tools** and **bundled skills** today (subagent and
middleware contributions are planned). Plugins run **in-process** with the
agent's privileges, so they're **disabled by default** and you opt in
explicitly — only enable plugins you trust.

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

`register` is called once at load. The registry accepts **five** contribution
types — a fork adds any of them as a plugin, never editing core `server.py`:

| Method | Contributes | Lifecycle |
|---|---|---|
| `register_tool(tool)` / `register_tools(iter)` | A LangChain tool | graph build (live-reloads) |
| `register_skill_dir(path)` | A `SKILL.md` directory | graph build |
| `register_router(router, prefix=None)` | A FastAPI `APIRouter` | **mounted once** at init (default prefix `/plugins/<id>`) |
| `register_surface(start, stop=None, name=None)` | A background surface (a Discord-style gateway) | `start` in startup, `stop` in shutdown |
| `register_subagent(config)` | A `SubagentConfig` (a delegate) | added to `SUBAGENT_REGISTRY` |

```python
def register(registry):
    registry.register_tool(hello)
    registry.register_router(_build_router())        # → GET /plugins/<id>/...
    registry.register_surface(_start, stop=_stop, name="my-surface")
    registry.register_subagent(_build_subagent())    # delegate via task/task_batch
```

**Routes + surfaces are wired once at process init and don't hot-reload** — a
config reload reuses them, so changing `plugins.enabled` needs a restart
(ADR 0018). Everything is best-effort: a failing plugin/route/surface logs and
never breaks boot. The shipped [`plugins/hello`](https://github.com/protoLabsAI/protoAgent/tree/main/plugins/hello)
example uses all five. Plugin contributions show in `GET /api/runtime/status`.

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
