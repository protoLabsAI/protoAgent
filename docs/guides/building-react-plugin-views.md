# Building a plugin view

A plugin can add a **console surface** — a rail icon that opens a view (a chart, an editor, a
dashboard, a generative-UI panel). The model is simple and **sandboxed**: the plugin **serves its
own page**, and the console renders it in an **iframe**. No host build, no shared bundle — so the
same plugin works whether it's bundled in the repo or **installed from a git URL** (ADR 0027).

> **Why iframes, not in-process React?** A plugin's UI is third-party code. The whole field
> sandboxes generated/third-party UI in iframes (Claude Artifacts, Open WebUI, CodePen). It's the
> right security boundary *and* it keeps plugins trivially distributable. (We tried Module
> Federation for in-process React; ADR 0038 retired it — heavier than a fork needs, less safe than
> untrusted code requires.)

## The shape

A plugin is a directory with a `protoagent.plugin.yaml` manifest and an `__init__.py` exposing
`register(registry)`. To add a view:

```yaml
# protoagent.plugin.yaml
id: mychart
name: My Chart
enabled: true
views:
  # A rail icon → an iframe of the page your plugin serves. placement: "right" docks it.
  - { id: mychart, label: "My Chart", icon: "BarChart3", placement: right, path: "/api/plugins/mychart/view" }
```

```python
# __init__.py
def _build_router():
    from fastapi import APIRouter
    from fastapi.responses import HTMLResponse
    router = APIRouter()

    @router.get("/data")          # your data API (gated, because it's under /api/*)
    async def _data() -> dict:
        return {"points": [1, 2, 3]}

    @router.get("/view")          # the page the console iframes
    async def _view():
        return HTMLResponse(_PAGE)

    return router

def register(registry):
    registry.register_router(_build_router(), prefix="/api/plugins/mychart")  # under /api → bearer-gated

_PAGE = """<!doctype html><html><body>...your UI...</body></html>"""
```

That's it. Drop the directory in `plugins/` (or `git`-install it) → the router mounts, the rail icon
appears, the iframe loads your page. **No host rebuild.**

## The console ↔ page bridge (ADR 0026)

After the iframe loads, the console `postMessage`s it `{ type: "protoagent:init", token, theme }`:

- **`token`** — the operator bearer. Send it on your fetches so authed `/api/*` calls work:
  `fetch(url, { headers: { Authorization: "Bearer " + token } })`.
- **`theme`** — console tokens (`bg`, `fg`, `fgMuted`, `border`, …). Apply them so your page matches
  the console — and default to dark so you never flash white.

```js
window.addEventListener("message", (e) => {
  if (e.data?.type !== "protoagent:init") return;
  const { token, theme } = e.data;        // use token for fetches; apply theme to your page
});
```

## Want React (or any framework)?

Build your UI however you like and **serve the built files** — inline (a single `_PAGE` string),
as static assets your plugin serves, or pull libs from a CDN at runtime (the **`artifact`** plugin
loads React + Babel from a CDN inside its sandbox). The console just iframes your `path`. For
untrusted code (generated artifacts, third-party views) keep the iframe `sandbox="allow-scripts"`
with **no** `allow-same-origin`.

## Reference plugins (in `plugins/`)

- **`artifact`** — generative UI: the agent calls `show_artifact(kind, code)`; the page renders
  HTML / SVG / Mermaid / React in a **nested sandboxed iframe**. The template for "render on demand."
- **`notes`** — a self-contained markdown editor: tools (`read_note`/`write_note`/`append_note`) +
  a `/note` data route + a `/view` editor page. The template for "a plugin that owns its vertical."

Both are pure Python + a served page + (optionally) a bundled `SKILL.md`, and both are fully
distributable from a git URL.

## Fork components (no plugin, no iframe)

If you're a **fork** and want first-party components compiled *into* the console (not sandboxed),
that's the build-time `src/ext/` seam — see ADR 0038 D3. Different from plugins, which are runtime +
sandboxed.

## References

- ADR 0026 (plugin console surfaces + the bridge), ADR 0027 (git-URL install), ADR 0038
  (the two-mode model + why federation was retired).
