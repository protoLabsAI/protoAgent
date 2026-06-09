---
name: building-plugins
description: >-
  Use this when asked to build, create, write, scaffold, or publish a protoAgent
  plugin — e.g. "make a plugin that …", "add a plugin for X", "package this as a
  plugin", "write a plugin that adds a tool/dashboard/workflow", "publish a plugin
  so others can install it". Covers the plugin contract (manifest + register()),
  the full contribution surface (tools, subagents, SKILL.md skills, workflows,
  console views, routes, MCP servers, config/secrets/settings), the conventional
  repo layout, testing, and distribution by git URL — with the safety model.
  Not for: using an already-installed plugin, or extending via a one-off SKILL.md
  skill or MCP server (smaller asks — see the Skills / MCP guides).
---

# Building a protoAgent plugin

A plugin is a self-contained directory (optionally its own git repo) that extends
a running agent **without forking** core. Authoritative refs: ADR 0018 (surfaces),
0019 (config/secrets/settings), 0026 (console views), 0027 (distribution); guides
`plugins`, `plugin-views`, `plugin-registry`. The shipped `plugins/hello/` is the
worked example — read it first.

## Scale to the ask
A one-tool plugin is ~15 lines (manifest + `register()`). A "full bundle"
(tools + subagents + skills + workflows + a console view + config) is a directory
of conventional subdirs. Build the smallest thing that satisfies the ask; don't
scaffold a dashboard for a single tool.

## 1. Decide what it contributes
Map the ask to the contribution surface:
- **tool / subagent / route / MCP server** → code, via `register(registry)`.
- **SKILL.md skills** / **`*.yaml` workflows** → data, auto-discovered from
  conventional `skills/` and `workflows/` subdirs (no code).
- **console view** (rail icon + page) → declared in the manifest `views:`; a **sandboxed
  iframe** of a page your plugin serves (ADR 0038).
- **events** (broadcast / react) → `registry.emit("x", data)` / `registry.on("topic.*", fn)`;
  declare `emits:` / `subscribes:` in the manifest (ADR 0039). Plugins coordinate via the bus,
  never by importing each other.
- **config / secrets / Settings fields** → declared in the manifest.
- **chat integration** (Discord/Slack/Telegram-style) → it's a *communication
  plugin* — use `scaffold_plugin(..., with_comms=True)` to get a `ChatAdapter`
  skeleton on the shared wirer (ADR 0029); you implement only connect/receive/send.
  See `docs/guides/communication-plugins.md` and `plugins/telegram` for a reference.

## 2. Lay out the directory
```
my-plugin/
  protoagent.plugin.yaml   # manifest (data — read without importing)
  __init__.py              # def register(registry): … (code contributions)
  skills/   <name>/SKILL.md # optional — auto-discovered
  workflows/ <name>.yaml    # optional — auto-discovered
```
Place it in `plugins/<id>/` (bundled with a fork) or install it from a git URL
into the live plugins dir (step 6).

## 3. Write the manifest (`protoagent.plugin.yaml`)
```yaml
id: my-plugin               # unique; must match the directory name
name: My Plugin
version: 1.0.0
enabled: false              # author default; operators opt in via plugins.enabled
config_section: my-plugin   # top-level YAML section it claims (NOT a list)
config: { api_base: "https://…" }      # defaults (ADR 0019)
secrets: [api_key]          # keys routed to secrets.yaml, never tracked YAML
settings:                   # render in Settings → its group
  - { key: api_base, label: "API base", type: string }
  - { key: api_key, label: "API key", type: secret }
views:                      # console rail view (ADR 0026/0038) — a sandboxed iframe of a page you serve
  - { id: board, label: "Board", icon: LayoutDashboard, path: /api/plugins/my-plugin/board }
emits: ["my-plugin.updated"]     # event-bus topics you broadcast (ADR 0039; optional, for discovery)
subscribes: ["other-plugin.*"]   # topics you listen for
requires_pip: ["httpx>=0.27"]   # deps — declared, NOT auto-installed (ADR 0027)
repository: https://github.com/owner/my-plugin
```

## 4. Write `register(registry)`
The registry collects code contributions (mounted once at init):
```python
def register(registry):
    cfg = registry.config                       # this plugin's resolved config (ADR 0019)
    registry.register_tool(my_tool)             # a LangChain @tool
    registry.register_subagent(my_subagent)     # a SubagentConfig
    registry.register_router(my_router, prefix="/api/plugins/my-plugin")  # gated FastAPI routes
    registry.register_mcp_server(my_factory)     # a managed MCP server
    registry.emit("updated", {"n": 1})          # broadcast on the bus → "my-plugin.updated" (ADR 0039)
    registry.on("other-plugin.*", on_event)     # react to another plugin without importing it
    # skills/ and workflows/ subdirs auto-load — no call needed.
```
A `views:` page is served by your router (e.g. `@router.get("/board")` returning HTML). Mount the
router under **`/api/plugins/<id>`** so it inherits the operator bearer gate; the console iframes the
page (sandboxed) and `postMessage`s it the bearer + theme — see the `plugin-views` guide. An event
under `<id>.*` lights your plugin's rail icon (a notification dot) until the user opens it.

## 5. Test it
- Enable it: `plugins: { enabled: [my-plugin] }`, restart, then
  `GET /api/runtime/status` → the plugin shows `loaded: true` with its tools/views.
- Unit-test the tool/registration like `tests/test_plugins.py` does.
- If it declares `requires_pip`, `python -m server plugin install-deps my-plugin`
  first (a missing dep gives a clear error on enable).

## 6. Distribute (optional)
Publish as a git repo; others install by URL:
`python -m server plugin install <git-url> --ref <tag>` (or the console Plugins
panel). Install pins a commit SHA in `plugins.lock`; `plugin sync` reproduces it.
**install ≠ enable ≠ trust** — installing only fetches code, never runs it; enabling
is the trust decision. For untrusted code, ship an MCP server instead (sandboxed).
Remove cleanly with `plugin uninstall <id>` (`--purge` also drops config + secrets).

## Gotchas (learned the hard way)
- `config_section` must be a **string**, never a list (reserved-section check).
- An `@tool`'s description comes from its **docstring** — use a plain string
  literal, not an f-string (`__doc__` is None for f-string "docstrings").
- Discovery reads the manifest as **data**; code runs only on **enable**. Keep the
  manifest importable-free.
- Adding routes/surfaces/views needs a **restart** (they mount once at init); the
  rail picks up a new view from `runtime-status` without a console rebuild.
- Don't edit core files to wire a plugin in — if you need to, you're missing a
  seam; file it (see the operator-fork contract) instead of re-porting each sync.
