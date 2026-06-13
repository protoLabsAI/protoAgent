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
0019 (config/secrets/settings), 0026 (console views), 0027 (distribution), 0045
(chat slot); guides `plugins`, `building-react-plugin-views`, `plugin-registry`.
The shipped `plugins/hello/` is the worked backend example — read it first. For a
**console view**, copy `examples/plugins/chat_example` — the gold-standard view
(the four rules + the init/theme handshake + slug-aware routing + the DS kit).

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
- **console view** (rail icon + page, or a `slot: "chat"` panel) → declared in the manifest
  `views:`; a **sandboxed iframe** of a page your plugin serves (ADR 0026/0038/0045). Copy
  `examples/plugins/chat_example`; full guide: `building-react-plugin-views`.
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
  # `path` MUST be a path a registered router serves, same-origin RELATIVE (no scheme/host).
  # The PAGE is public (an iframe load can't carry a bearer); its DATA calls are gated /api/.
  - { id: board, label: "Board", icon: LayoutDashboard, path: /plugins/my-plugin/board }
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
A `views:` page is served by your router (e.g. `@router.get("/board")` returning HTML). Mount **data**
routes under **`/api/plugins/<id>`** so they inherit the operator bearer gate; serve the **page itself**
under the public **`/plugins/<id>`** prefix (an iframe page-load can't carry a bearer, so the page is
public chrome — its data calls are the gated part). The console iframes the page (sandboxed) and
`postMessage`s it the bearer + theme; the page derives a slug-aware base and uses the DS kit's
`apiFetch` for authed same-origin calls — copy `examples/plugins/chat_example`, full guide
`building-react-plugin-views`. An event under `<id>.*` lights your plugin's rail icon (a notification
dot) until the user opens it.

## 5. Test it — live, no restart
You don't need to restart to try a plugin you built. With **plugin-devkit** enabled:
- `scaffold_plugin(...)` already **enabled** it (the default) — its tools/view are
  live on your **next turn**. Call its `<id>_hello` to confirm.
- Iterate: edit the plugin's `__init__.py`, then call **`reload_plugins`** — the
  loader re-execs the file, so your change is live next turn (no restart).
- Built a plugin some other way (CLI / by hand)? Call **`enable_plugin("<id>")`** to
  turn it on + hot-reload, or toggle it in the console Plugins panel (#822).
- `GET /api/runtime/status` → the plugin shows `loaded: true` with its tools/views.
- Unit-test the tool/registration like `tests/test_plugins.py` does.
- If it declares `requires_pip`, `python -m server plugin install-deps my-plugin`
  first (a missing dep gives a clear error on enable).

For a **standalone-repo** plugin (its own git repo, not bundled in protoAgent), pass
`with_tests=True` (`scaffold_plugin`) / `--tests` (the CLI) to also get a **host-free
test suite + CI + requirements-dev + pyproject** so the repo is green from birth: the
suite imports the plugin with no host (lazy host imports + a fake registry) and `ruff`
+ `pytest` run in GitHub Actions. Keep host-only imports (`fastapi`, `graph.*`) inside
`register()` so the suite needs only `requirements-dev.txt`.

From the shell (no agent): `python -m server plugin new "My Plugin" --view --skill --tests`
scaffolds the skeleton; `plugin new-bundle "My Stack" --member id=url@ref --builtin delegates`
scaffolds an ADR-0040 bundle.

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
- **Enabling** a plugin is fully live: tools/subagents/middleware/MCP rebuild with
  the graph and its router (which serves any view) **hot-mounts** (#822) — no restart.
  Only **disabling** leaves a stale route behind (FastAPI can't unmount), so that path
  alone wants a restart. The rail picks up a new view from `runtime-status` without a
  console rebuild.
- Don't edit core files to wire a plugin in — if you need to, you're missing a
  seam; file it (see the operator-fork contract) instead of re-porting each sync.
