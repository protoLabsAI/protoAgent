# Plugin view bridge (wire protocol)

A console view is a **sandboxed iframe** ([ADR 0026](/adr/0026-plugin-contributed-console-surfaces)),
so `postMessage` is the only channel between your page and the console. This page is the wire
contract: every message, its direction, its payload, and the guarantees around it.

Most views should use the **plugin kit** (`/_ds/plugin-kit.js`) rather than these messages directly —
see [Building a plugin view](/guides/building-react-plugin-views). Read this when you need to know
what the kit is doing, implement a capability it doesn't wrap, or debug a view that isn't receiving
what you expect.

::: tip Origin
The console posts to the iframe's **resolved origin**, and ignores any message whose `source` isn't
this iframe's `contentWindow`. A view served from an absolute URL escapes that origin and the
handshake silently never completes — which is why `views[].path` must be a same-origin relative path
(the manifest parser warns about this; see [`views`](/reference/plugin-manifest#field-views)).
:::

## Messages at a glance

| Message | Direction | Payload |
|---|---|---|
| `protoagent:init` | host → page | `{ token: string \| null, theme: object }` |
| `protoagent:ready` | page → host | *(none)* |
| `protoagent:theme` | host → page | `{ theme: object }` |
| `protoagent:subscribe` | page → host | `{ patterns: string[], since?: number, background?: boolean }` |
| `protoagent:event` | host → page | `{ topic: string, data: object, seq?: number }` |
| `protoagent:publish` | page → host | `{ topic: string, data?: object }` |
| `protoagent:keybindings` | page → host | `{ bindings: [{ id, keys, label?, group? }] }` |
| `protoagent:keybinding` | host → page | `{ id: string }` |
| `protoagent:keydown` | page → host | `{ combo: string, editable?: boolean }` |
| `protoagent:contextmenu:register` | page → host | `{ items: MenuItem[] }` |
| `protoagent:contextmenu:open` | page → host | `{ x: number, y: number, items?: MenuItem[] }` |
| `protoagent:contextmenu:action` | host → page | `{ itemId: string }` |

## The handshake — `init` / `ready`

The console posts `protoagent:init` with the operator's bearer token and the live theme once the
iframe loads. **The token is never in the URL** — a URL leaks through history, logs, and referrers.

```js
window.addEventListener("message", (e) => {
  if (e.data?.type === "protoagent:init") {
    token = e.data.token;   // may be null when the deployment is unauthenticated
    applyTheme(e.data.theme);
  }
});
parent.postMessage({ type: "protoagent:ready" }, "*");   // ← do this
```

**Post `protoagent:ready` as soon as you are listening.** The console's load-time `init` can arrive
*before* your handler is registered — a page that dynamically imports the kit is a frame or two late,
and the message is simply dropped, leaving the view unauthenticated and on the wrong theme. `ready`
tells the host to re-send. It is idempotent, and there is a timed retry as a fallback for older kits
that never ping, but relying on the retry means a visible flash of the default theme.

## Theme — `protoagent:theme`

Sent on every live operator theme switch, carrying the same `theme` shape as `init`. Handle it, or
your view stays on the theme it booted with while the console around it changes. The kit's
`initPluginView()` maps both `init` and `theme` onto the DS `--pl-*` custom properties for you.

## Events — `subscribe` / `event` / `publish`

The event bus ([ADR 0039](/adr/0039-plugin-event-bus)) relayed across the sandbox boundary.

```js
parent.postMessage({ type: "protoagent:subscribe", patterns: ["artifact.#"] }, "*");

window.addEventListener("message", (e) => {
  if (e.data?.type === "protoagent:event") {
    lastSeq = e.data.seq ?? lastSeq;    // your high-water mark
    handle(e.data.topic, e.data.data);
  }
});
```

- **`patterns` replace the previous set** — re-subscribing is not additive. `topicMatches` semantics
  apply (`#` is the multi-segment wildcard).
- **`since` replays what you missed.** Pass your last `seq` and the host immediately replays retained
  ring-buffer frames newer than it, oldest→newest, before live delivery resumes. This is what lets a
  remounted view catch up instead of polling. Your `since` is authoritative: the host resets its
  dedupe mark to it, so you will not see the same `seq` twice across a replay.
- **`background: true` keeps you mounted while hidden.** Normally only the *visible* plugin's iframe
  is mounted, so a hidden view receives nothing. Only an explicit boolean changes the mount policy —
  omitting the field leaves it alone.
- **`publish` is namespace-forced.** The host rewrites your topic to `<pluginId>.<name>` before it
  reaches the bus, so a page can only ever publish under its own plugin's id.

## Keybindings — `keybindings` / `keybinding` / `keydown`

A sandboxed iframe breaks keyboard shortcuts in **both** directions: keys pressed inside it never
reach the console's window listener, and the page can't call `registerKeybinding`
([ADR 0063](/adr/0063-keybinding-system), [#1457](https://github.com/protoLabsAI/protoAgent/issues/1457)).

```js
parent.postMessage({ type: "protoagent:keybindings", bindings: [
  { id: "toggle", keys: "mod+shift+p", label: "Toggle panel", group: "My Plugin" },
]}, "*");

window.addEventListener("message", (e) => {
  if (e.data?.type === "protoagent:keybinding" && e.data.id === "toggle") togglePanel();
});
```

- **Ids are forced into `plugin.<pluginId>.<your id>`**, so a page cannot register or silently
  replace a core binding like `chat.new`, or collide with another plugin. The id echoed back to you
  is *your* local one, not the namespaced one.
- **The chord you name is a default.** The operator's override wins, through the same
  Settings ▸ Keyboard path as every other binding.
- **Re-registering replaces the whole set** (`bindings: []` clears it), so a chord you drop can't
  linger as a ghost firing into a page that forgot about it.
- Malformed entries are dropped individually rather than failing the batch; ids must match
  `[a-zA-Z0-9._-]+`; at most **32** bindings are accepted; on duplicate ids the first wins.

Forward chords you don't handle so global shortcuts keep working while your view has focus:

```js
parent.postMessage({ type: "protoagent:keydown", combo, editable }, "*");
```

Set `editable: true` when focus is in one of your own text fields — the host honours it, so ⌘K
doesn't fire out from under someone typing in your search box. Absent means `false`.

## Context menus — `contextmenu:register` / `:open` / `:action`

A right-click inside the frame never bubbles to the console, so the page has to report it
([ADR 0036](/adr/0036-context-menu-system), [#3030](https://github.com/protoLabsAI/protoAgent/issues/3030)).

```js
parent.postMessage({ type: "protoagent:contextmenu:register", items: [
  { id: "rename", label: "Rename" },
  { divider: true },
  { id: "delete", label: "Delete", danger: true },
]}, "*");

document.addEventListener("contextmenu", (e) => {
  e.preventDefault();
  parent.postMessage({ type: "protoagent:contextmenu:open", x: e.clientX, y: e.clientY }, "*");
});

window.addEventListener("message", (e) => {
  if (e.data?.type === "protoagent:contextmenu:action") run(e.data.itemId);
});
```

- `register` declares the **default** set; `open` may carry its own `items` for a menu specific to
  whatever is under the cursor, or omit them to use the registered set.
- Coordinates are your own `clientX/clientY` — the host translates them through the frame's rect. A
  missing or non-finite coordinate falls back to the frame's top-left rather than refusing to open:
  the operator asked for a menu, and a slightly misplaced one beats none.
- Item ids are namespaced like keybinding ids, and re-registering replaces the set (`items: []`
  clears it). Items support `label`, `icon`, `danger`, `disabled`, and `{ divider: true }`; a
  trailing divider is dropped.

## The kit

`/_ds/plugin-kit.js` is served same-origin by the console and wraps the handshake, theming, and
authed fetch. Import it as a module — the `window.protoPluginView` global only exists after
evaluation, so a separate classic `<script>` can't rely on it.

| Helper | What it does |
|---|---|
| `initPluginView(onInit?)` | Listens for `init` **and** live `theme`, maps the console theme onto the DS `--pl-*` tokens, and fires `onInit({ token, theme })` on both. Call once on load. |
| `getToken()` | The captured bearer — `null` until the handshake delivers one. |
| `apiUrl(path)` | Resolves a root-relative path to its **slug-aware** URL (prepends `/agents/<slug>` under the fleet proxy). Idempotent. |
| `apiFetch(input, init?)` | Same-origin `fetch` through `apiUrl`, with `Authorization: Bearer <token>` attached when present. Use it for every gated `/api/…` call. |

Related: [Building a plugin view](/guides/building-react-plugin-views) ·
[Plugin manifest ▸ `views`](/reference/plugin-manifest#field-views) ·
[Plugin registry API](/reference/plugin-registry-api)
