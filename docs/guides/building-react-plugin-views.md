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

## Events — broadcast and subscribe (ADR 0039)

Plugins talk to the rest of the app through the **event bus**, never by importing each other. You
broadcast and forget; anyone who cares subscribes by topic. Topics are namespaced to your plugin
(`<plugin_id>.<event>`), and you may only publish under your own namespace.

**From Python** (in `register`):

```python
def register(registry):
    registry.emit("created", {"id": "a1"})     # publishes "<plugin_id>.created"
    registry.on("notes.*", lambda evt: ...)    # subscribe to ANY topic (read-only); * / # wildcards
```

**From a sandboxed view** (your served page), over the bridge:

```js
// subscribe to topics you care about, then receive them
parent.postMessage({ type: "protoagent:subscribe", patterns: ["artifact.#"] }, "*");
window.addEventListener("message", (e) => {
  const m = e.data || {};
  if (m.type === "protoagent:event") { /* m.topic, m.data */ }
});
// publish (the host forces your namespace + gates it)
parent.postMessage({ type: "protoagent:publish", topic: "created", data: { id: "a1" } }, "*");
```

**Declare your contract** in the manifest so others can discover it (shown in runtime status):

```yaml
emits: ["artifact.created"]
subscribes: ["notes.changed"]
```

**Notification dots come for free:** any event under `<plugin_id>.*` lights your plugin's rail icon
until the user opens that surface — no badge endpoint, no polling. (See the
[security & trust model](../explanation/security-and-trust.md): subscribing is safe; publishing is
the gated direction.)

## Want React (or any framework)?

Build your UI however you like and **serve the built files** — inline (a single `_PAGE` string),
as static assets your plugin serves, or pull libs from a CDN at runtime (the **`artifact`** plugin
loads React + Babel from a CDN inside its sandbox). The console just iframes your `path`. For
untrusted code (generated artifacts, third-party views) keep the iframe `sandbox="allow-scripts"`
with **no** `allow-same-origin`.

## Reference plugins (in `plugins/`)

- **artifact-plugin** (external — github.com/protoLabsAI/artifact-plugin) — generative UI: the agent calls `show_artifact(kind, code)`; renders in a nested sandboxed iframe. The reference distributable plugin.
- **`notes`** — a self-contained markdown editor: tools (`read_note`/`write_note`/`append_note`) +
  a `/note` data route + a `/view` editor page. The template for "a plugin that owns its vertical."

Both are pure Python + a served page + (optionally) a bundled `SKILL.md`, and both are fully
distributable from a git URL.

## Fork components (no plugin, no iframe)

If you're a **fork** and want first-party components compiled *into* the console (not sandboxed),
that's the build-time `src/ext/` seam — see ADR 0038 D3. Different from plugins, which are runtime +
sandboxed.

## References

- [Security & trust model](../explanation/security-and-trust.md) — why plugin UIs are sandboxed
  iframes, and the "installing a plugin runs its code" trust posture.
- ADR 0026 (plugin console surfaces + the bridge), ADR 0027 (git-URL install), ADR 0038
  (the two-mode model + why federation was retired).
