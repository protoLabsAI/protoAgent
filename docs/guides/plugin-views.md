# Plugin console views (rail surfaces)

A plugin can add its own **left-rail icon and view** to the operator console — a
dashboard, board, or whatever UI the fork wants — by declaring it in the manifest
and serving a page. **No console rebuild.** This is the frontend counterpart to
[plugin tools/routes](/guides/plugins) and [plugin settings](/guides/plugins);
see [ADR 0026](/adr/0026-plugin-contributed-console-surfaces).

## Declare a view

Add a `views:` block to `protoagent.plugin.yaml`:

```yaml
views:
  - id: board                      # unique within the plugin
    label: "Board"                 # rail + tab label
    icon: LayoutDashboard          # a lucide-react icon name
    path: /plugins/myplugin/board  # the page the iframe loads (you serve it)
    tabs:                          # optional sub-nav (view-tabs)
      - { id: open, label: "Open", path: /plugins/myplugin/board?tab=open }
      - { id: done, label: "Done", path: /plugins/myplugin/board?tab=done }
```

The console reads this from `/api/runtime/status` and renders a rail icon per
view (keyed `plugin:<id>:<viewId>`). When selected, it hosts `path` in a
same-origin **iframe** that fills the stage; `tabs` render as a sub-nav that swaps
the iframe page. `icon` is a [lucide](https://lucide.dev) name from the console's
allowlist — covering dashboards, data, comms, dev, AI, finance, space/fleet, and
security (e.g. `LayoutDashboard`, `BarChart3`, `Database`, `Workflow`, `Bot`,
`Rocket`, `Satellite`, `Coins`, `Shield`); an unknown name falls back to a generic
plugin glyph.

## Serve the page

The page is yours — any framework, or plain HTML. Serve it from the plugin's
router (the same `register_router` that backs tools/routes):

```python
def _build_router():
    from fastapi import APIRouter
    from fastapi.responses import HTMLResponse
    router = APIRouter()

    @router.get("/board")          # mounted at /plugins/myplugin/board
    async def _board():
        return HTMLResponse("<!doctype html>… your UI …")
    return router

def register(registry):
    registry.register_router(_build_router())
```

See the shipped [`plugins/hello`](https://github.com/protoLabsAI/protoAgent/tree/main/plugins/hello)
for a worked example (a `views:` entry + a `/view` page).

## The init handshake (bearer + theme)

After the iframe loads, the console **posts a message** to it — so your page gets
the operator bearer (for its own API calls) and the console theme tokens (to match
the look) **without a token in the URL**:

```js
window.addEventListener("message", (e) => {
  const m = e.data || {};
  if (m.type !== "protoagent:init") return;
  // m.token — operator bearer (or null when none is configured); use it as
  //           `Authorization: Bearer <token>` for your /plugins/<id>/... calls.
  // m.theme — { bg, bgPanel, fg, fgMuted, brand, border } from the console.
  if (m.theme?.bg) document.body.style.background = m.theme.bg;
});
```

The message is sent same-origin and targeted at your page's origin.

## Trust & sandbox

The view runs in an iframe with `sandbox="allow-scripts allow-forms allow-same-origin"`.
This scopes the plugin's CSS/JS from the console — it is **not** a security
boundary against a malicious enabled plugin: an enabled plugin already runs
in-process as the agent (same trust model as [plugin backends](/guides/plugins)).
Only enable plugins you trust.

## Lifecycle

- Views appear for **enabled** plugins; disabling one (or a config reload that
  drops it) removes its rail icon and, if you were on it, falls back to Chat.
- Mounting/serving is config-driven — adding a view to an existing plugin needs a
  restart (routes mount once at init), but the rail picks up the declaration from
  `runtime-status` with no console rebuild.
