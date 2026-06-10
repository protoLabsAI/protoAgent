# Build a plugin view

A plugin adds a console surface by **serving its own page** and declaring it in the manifest; the
console renders it in a sandboxed **iframe**. No host build. This is the short, copy-me entry — the
full guide is **[Building a plugin view](/guides/building-react-plugin-views)**.

## Copy the gold-standard

[`examples/plugins/chat_example`](https://github.com/protoLabsAI/protoAgent/tree/main/examples/plugins/chat_example)
is a single-page view that follows every rule below — start by copying it:

```bash
cp -r examples/plugins/chat_example plugins/
```

```yaml
plugins:
  enabled: [chat_example]
```

Reload. (`chat_example` claims the chat slot; a normal view adds a rail icon instead.)

## The four rules

1. **Serve the path you declare** — the manifest's `views[].path` MUST equal a path your
   `register_router` serves (default prefix `/plugins/<id>`; a custom prefix is fine, just keep them
   in sync). A mismatch is a blank iframe.
2. **Gate by default** — mount data routes under `prefix="/api/plugins/<id>"` so they inherit the
   operator bearer gate. Use the ungated `/plugins/<id>` prefix only for genuinely public assets (the
   page itself — an iframe page-load can't carry an `Authorization` header).
3. **Same-origin, slug-aware, never hardcode** — derive `base = location.pathname.split("/plugins/")[0]`
   and prefix every fetch/asset. Never hardcode `/api/.../`, `/plugins/.../`, or `http://localhost:PORT`
   — it breaks the `/agents/<slug>/` fleet proxy and the same-origin token handshake.
4. **Link the DS kit** — `<base>/_ds/plugin-kit.css` + `<base>/_ds/plugin-kit.js` instead of
   hardcoding hex or a CDN. The kit's `--pl-*` tokens re-skin to the operator's live theme.

## The kit helper API (`plugin-kit.js`)

The console serves the design-system kit same-origin at `<base>/_ds/plugin-kit.{css,js}`. Load it as
an ES module (`import { initPluginView } from ".../plugin-kit.js"`) or use the
`window.protoPluginView` global from a classic script tag:

| Helper | What it does |
|---|---|
| `initPluginView(onInit?)` | Listens for the `protoagent:init` handshake (bearer + theme) **and** live `protoagent:theme` re-themes, mapping the console theme onto the DS `--pl-*` tokens. Call once on load. |
| `getToken()` | The captured operator bearer (null until the handshake delivers one). |
| `apiFetch(input, init?)` | Same-origin `fetch` with `Authorization: Bearer <token>` attached when present — for every gated `/api/...` call. |
| `window.protoPluginView` | The same three helpers as a global, for no-build pages. |

```js
const kit = window.protoPluginView;
kit.initPluginView();                                   // handshake + live re-theme, hands-free
const base = location.pathname.split("/plugins/")[0];   // "" or "/agents/<slug>"
const res = await kit.apiFetch(base + "/api/plugins/mychart/data");
```

## Next

- **[Building a plugin view](/guides/building-react-plugin-views)** — the full guide: the `slot: "chat"`
  panel (ADR 0045), the event-bus bridge (ADR 0039), the sandbox split (ADR 0026 D6), and references.
- **[Plugins](/guides/plugins)** — the rest of the plugin contract (tools, subagents, config, MCP).
