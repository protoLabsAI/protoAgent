# Building a plugin view

A plugin can add a **console surface** — a rail icon that opens a view (a chart, an editor, a
dashboard, a generative-UI panel), or a view that **replaces** the built-in chat panel
(`slot: "chat"`, ADR 0045). The model is simple and **sandboxed**: the plugin **serves its own
page**, and the console renders it in an **iframe**. No host build, no shared bundle — so the same
plugin works whether it's bundled in the repo or **installed from a git URL** (ADR 0027).

> **Why iframes, not in-process React?** A plugin's UI is third-party code. The whole field
> sandboxes third-party/generated UI in iframes (Claude Artifacts, Open WebUI, CodePen). It's the
> right boundary *and* it keeps plugins trivially distributable. (We tried Module Federation for
> in-process React; ADR 0038 retired it — heavier than a fork needs, less safe than untrusted code
> requires. Forks that want native in-process components use the build-time `src/ext/` seam instead —
> see [below](#fork-components-no-plugin-no-iframe).)

> [!TIP]
> **Copy the gold-standard.** [`examples/plugins/chat_example`](https://github.com/protoLabsAI/protoAgent/tree/main/examples/plugins/chat_example)
> is a single-page vanilla-JS view that follows every rule below — the init/theme handshake, live
> re-theming, slug-aware routing, the DS kit, and a real turn over a gated `/api/` route. Start by
> copying it: `cp -r examples/plugins/chat_example plugins/`.

## The four rules

Every plugin view should follow these. Each links a section below.

| Rule | One-line why |
|---|---|
| **1. Serve the path you declare** — the manifest's `views[].path` MUST equal a path your `register_router` actually serves. | The console iframes exactly that path. Default router prefix is `/plugins/<id>`; a custom prefix (as the artifact plugin uses) is fine — just keep `path` in sync with it. A mismatch is a blank iframe. |
| **2. Gate by default** — mount the router under `prefix="/api/plugins/<id>"`. | Routes under `/api/*` inherit the operator **bearer gate** (ADR 0026 D5). Use the ungated `/plugins/<id>` prefix **only** for genuinely public assets (e.g. the page itself — see [why the page is public](#why-the-page-is-public-but-its-data-is-gated)). |
| **3. Same-origin, slug-aware, never hardcode** — for DATA calls just use the kit's slug-aware `apiFetch`/`apiUrl` (plugin-kit 0.26+): `kit.apiFetch("/api/plugins/<id>/x")` resolves itself. Only the kit's OWN `<link>`/`<script>` need a hand-derived `base = location.pathname.split("/plugins/")[0]` — they load *before* the kit exists. | Never hardcode an absolute `/api/.../`, `/plugins/.../`, or `http://localhost:PORT`. On the host window `base=""`; through the ADR 0042 `/agents/<slug>/` fleet proxy `base="/agents/<slug>"`. `apiFetch` reaches the right agent on both; hardcoding talks to the wrong agent and breaks the same-origin token handshake. |
| **4. Link the DS kit** — load `<base>/_ds/plugin-kit.css` + `<base>/_ds/plugin-kit.js`. | Pull colors/components/handshake from the console's own design system instead of hand-rolling a hex map or pinning a CDN. The kit's `--pl-*` tokens re-skin to the operator's live theme for free. See [the kit helpers](#the-kit-helpers-plugin-kitjs). |

## The shape

A plugin is a directory with a `protoagent.plugin.yaml` manifest and an `__init__.py` exposing
`register(registry)`. To add a view, declare it and serve a page.

```yaml
# protoagent.plugin.yaml
id: mychart
name: My Chart
enabled: true
# A rail icon → an iframe of the page your plugin serves. placement: "right" docks it
# into the right sidebar; "rail" (default) is a full left-rail surface.
views:
  - { id: mychart, label: "My Chart", icon: "BarChart3", placement: right, path: "/plugins/mychart/view" }
```

```python
# __init__.py
def _build_router():
    from fastapi import APIRouter
    from fastapi.responses import HTMLResponse
    router = APIRouter()

    @router.get("/data")          # RULE 2: a DATA route — gate it under /api/plugins/<id>
    async def _data() -> dict:
        return {"points": [1, 2, 3]}

    @router.get("/view")          # the page the console iframes (public chrome — see below)
    async def _view():
        return HTMLResponse(_PAGE)

    return router

def register(registry):
    # RULE 1 + 2: serve the page on the path you declared. Data routes go under /api/.
    registry.register_router(_data_router(), prefix="/api/plugins/mychart")   # gated data
    registry.register_router(_build_router(), prefix="/plugins/mychart")      # public page
```

That's it. Drop the directory in `plugins/` (or `git`-install it) → the router mounts, the rail icon
appears, the iframe loads your page. **No host rebuild.**

`icon` is **any** [lucide](https://lucide.dev) icon name — PascalCase (`LineChart`) or kebab-case
(`line-chart`). A curated common set (`LayoutDashboard`, `BarChart3`, `Database`, `Workflow`, `Bot`,
`Rocket`, `Coins`, `Shield`, …) renders instantly; anything else lazy-loads on demand. The console
reads `views` from `/api/runtime/status` and renders a rail icon per view (keyed
`plugin:<id>:<viewId>`); `tabs` render as a sub-nav that swaps the iframe page.

### Why the page is public but its data is gated

A browser **iframe page-load can't carry an `Authorization` header** — so the HTML page itself must
be reachable without the bearer. Serve **only the page** outside `/api/` (the public `/plugins/<id>`
prefix); everything the page *fetches* (your data) goes through gated `/api/plugins/<id>/...` routes,
authed with the bearer the console hands you over the [init handshake](#the-init-handshake-bearer--theme).
The page is public chrome; its data is not.

This holds **through the fleet proxy** too (#1890): when the console views a member, the page loads
at `/agents/<slug>/plugins/<id>/…` and a token-gated hub defers the public decision to the *member's*
own auth-exempt list (served on `/.well-known/protoagent/public-paths`, TTL-cached). You don't have
to do anything — your view's page path is auto-exempted from the manifest on the instance that runs
the plugin, and the hub honors that instance's list.

## Claim the chat slot (`slot: "chat"`)

The chat surface is a **slot** (ADR 0045): your view can *replace* the built-in chat panel instead of
adding a rail icon.

```yaml
views:
  - { id: panel, label: "My chat", icon: MessageSquare, path: "/plugins/mychat/panel", slot: chat }
```

What changes versus a normal view:

- Your page renders under the core **Chat** rail id — **no separate icon**; the first enabled
  claimant wins.
- You inherit chat's mount contract: the iframe stays mounted for the **app's lifetime** (a normal
  view unmounts when you switch away) — what keeps an in-flight streamed turn alive across surface
  switches.
- Without a claimant, the built-in chat renders — the console is never chat-less.

Your page speaks the same protocol the built-in panel does: the
[init handshake](#the-init-handshake-bearer--theme) hands you the bearer + theme, and the agent is
driven over **A2A 1.0** (`SendStreamingMessage` for the streaming turn, `tasks/get` for
reconciliation) plus three REST endpoints (`/api/chat/commands`, `DELETE /api/chat/sessions/{id}`,
non-streaming `POST /api/chat`). Before shipping a real one, read the **conformance checklist in
ADR 0045** — it encodes the hard-won invariants (never remount mid-turn, reconcile stuck streams on
load, render terminal text once, key local state per agent slug).

`examples/plugins/chat_example` is the worked reference: a vanilla-JS panel that does the handshake,
live re-theming, slug-aware routing, and a turn over the **non-streaming** `/api/chat` fallback. Copy
it and grow your panel from there:

```bash
cp -r examples/plugins/chat_example plugins/
```

```yaml
plugins:
  enabled: [chat_example]
```

Reload — Chat becomes the example panel. Disable (or delete) the copy and the built-in chat is back.

Forks have an in-process alternative: register a `src/ext` surface with `id: "chat"` (ADR 0038 D3) —
it overrides the slot ahead of plugin claims, with full React context.

## The init handshake (bearer + theme)

After the iframe loads, the console **posts a message** to it — so your page gets the operator bearer
(for its own API calls) and the console theme tokens (to match the look) **without a token in the
URL**. There are two messages:

- **`protoagent:init`** — sent once after load, carrying `{ token, theme }`.
- **`protoagent:theme`** — sent on every live operator theme switch, carrying the new `{ theme }`, so
  your page re-skins without a reload.

```js
window.addEventListener("message", (e) => {
  const m = e.data || {};
  if (m.type === "protoagent:init") {
    // m.token — operator bearer (or null when none is configured). Use it as
    //           `Authorization: Bearer <token>` on your /api/plugins/<id>/... calls.
    // m.theme — { bg, bgPanel, fg, fgMuted, brand, border } from the console.
    if (m.theme?.bg) document.body.style.background = m.theme.bg;
  } else if (m.type === "protoagent:theme") {
    // live re-theme — re-apply m.theme
  }
});
```

The message is sent same-origin and targeted at your page's origin. In practice you don't hand-write
this — [the DS kit](#the-kit-helpers-plugin-kitjs) wires it for you and maps `theme` onto its `--pl-*`
tokens.

## The kit helpers (`plugin-kit.js`)

The console serves the design-system kit same-origin at **`<base>/_ds/plugin-kit.{css,js}`** — the
no-hardcode escape hatch. `plugin-kit.css` gives `--pl-*` tokens + `.pl-*` components;
`plugin-kit.js` does the handshake and exposes a tiny API.

Link both slug-aware (RULE 3 + 4). **`plugin-kit.js` is an ES module** (`export` statements) — a
classic `<script src>` throws `Unexpected token 'export'` and never runs it, so the JS half loads
with a **dynamic `import()`** (a static import specifier can't carry the slug-aware base; only the
CSS `<link>` is base-prefixed by hand, because it loads before any script can help):

```html
<script>
  // RULE 3: compute base FIRST — everything (the kit href included) prefixes it.
  window.__base = location.pathname.split("/plugins/")[0];  // "" or "/agents/<slug>"
  document.write('<link rel="stylesheet" href="' + window.__base + '/_ds/plugin-kit.css">');
</script>
...
<script type="module">
  const kit = await import(window.__base + "/_ds/plugin-kit.js");
  // …the view's logic, using kit.initPluginView() / kit.apiFetch() …
</script>
```

The API:

| Helper | What it does |
|---|---|
| `initPluginView(onInit?)` | Starts listening for the handshake. Maps the console `theme` onto the DS `--pl-*` tokens on the initial `protoagent:init` **and** on every live `protoagent:theme` re-theme. `onInit({ token, theme })` (optional) fires on both. Call once on load. |
| `getToken()` | The captured operator bearer (null until the handshake delivers one). |
| `apiUrl(path)` | Resolves a root-relative `path` to its **slug-aware** URL — prepends `/agents/<slug>` under the fleet proxy, leaves it untouched on the host window (plugin-kit 0.26+). Idempotent: a path already under the base is returned as-is. |
| `apiFetch(input, init?)` | A same-origin `fetch` that resolves `input` through `apiUrl` (**slug-aware** — pass a bare `/api/...`, no manual base) **and** attaches `Authorization: Bearer <token>` when present. Use it for every gated `/api/...` call. |

So a whole view's handshake + authed fetch is:

```js
const kit = await import(window.__base + "/_ds/plugin-kit.js");
kit.initPluginView();                         // handshake + live re-theme, hands-free
const res = await kit.apiFetch("/api/chat", { method: "POST", body: ... });  // slug-aware, no manual base
```

> The kit also assigns a `window.protoPluginView` global when it runs, but that only happens after
> the module is evaluated — don't rely on it from a separate classic `<script>`; import the module
> and use what it returns.

Prefer the kit over hardcoding hex values, a theme map, or a CDN — colors and the handshake both come
from the console's own DS, so your view always matches the operator's live theme.

## Events — broadcast and subscribe (ADR 0039)

Plugins talk to the rest of the app through the **event bus**, never by importing each other. You
broadcast and forget; anyone who cares subscribes by topic. Topics are namespaced to your plugin
(`<plugin_id>.<event>`), and you may only publish under your **own** namespace (the host forces it).

**From Python** (in `register`):

```python
def register(registry):
    registry.emit("created", {"id": "a1"})     # publishes "<plugin_id>.created"
    registry.on("notes.*", lambda evt: ...)    # subscribe to ANY topic (read-only); * / # wildcards
```

**From a sandboxed view** (your served page), over the bridge — three message types:

```js
// 1. subscribe to topics you care about, then receive them
parent.postMessage({ type: "protoagent:subscribe", patterns: ["artifact.#"] }, "*");
window.addEventListener("message", (e) => {
  const m = e.data || {};
  if (m.type === "protoagent:event") { /* m.topic, m.data, m.seq */ } // 2. delivered events
});
// 3. publish — the host FORCES your plugin's namespace + gates it
parent.postMessage({ type: "protoagent:publish", topic: "created", data: { id: "a1" } }, "*");
```

**Declare your contract** in the manifest so others can discover it (surfaced in runtime status):

```yaml
emits: ["mychart.created"]
subscribes: ["notes.changed"]
```

**Notification dots come for free:** any event under `<plugin_id>.*` lights your plugin's rail icon
until the user opens that surface — no badge endpoint, no polling. Subscribing is always safe;
publishing is the gated direction (namespace-forced).

### Replay and hidden delivery (#1640)

By default your iframe exists **only while its surface is visible** — switch away and the page is
torn down; unseen activity becomes a rail dot. Two subscribe options refine that for event-driven
views:

```js
parent.postMessage({
  type: "protoagent:subscribe",
  patterns: ["boardy.#"],
  since: lastSeq,        // optional — replay retained events with seq > lastSeq, NOW
  background: true,      // optional — keep this view mounted + receiving while hidden
}, "*");
```

- **`seq` on every event.** Each `protoagent:event` carries the bus sequence number. Track the
  highest one you've applied — it's your high-water mark.
- **`since` — replay on subscribe.** When present, the host immediately relays the retained
  events **newer than that seq** that match your patterns (from the console's mirror of the
  server ring buffer — the same catch-up the console itself uses on SSE reconnect), then
  continues live with **no gap and no duplicate** (the host dedupes by seq). `since: 0` means
  "everything you still have". Replay is **best-effort**, exactly like the server ring: seqs
  older than the retention horizon (or from before this console tab connected) are gone —
  treat an empty replay after a long absence as "do one full state fetch", not as "nothing
  happened".
- **`background: true` — hidden delivery.** Per-subscribe opt-in: the console keeps your view
  mounted (hidden) when the operator switches away, so events keep flowing to your live model
  (a map, a running chart). Your page is **not** reloaded on re-open. `background: false`
  opts back out; omitting the field never changes the current mode. The notification dot
  still lights either way. Use it only when you genuinely keep state — a hidden iframe still
  costs memory and timers; session-scoped, so ask on every load.

**The recommended pattern** for a dashboard that used to poll:

```js
let lastSeq = Number(sessionStorage.getItem("myview.seq") || 0);
const subscribe = () => parent.postMessage(
  { type: "protoagent:subscribe", patterns: ["myplugin.#"], since: lastSeq }, "*");
window.addEventListener("message", (e) => {
  const m = e.data || {};
  if (m.type !== "protoagent:event") return;
  apply(m.topic, m.data);                       // update your model
  if (typeof m.seq === "number") { lastSeq = m.seq; sessionStorage.setItem("myview.seq", String(m.seq)); }
});
subscribe();
```

Your plugin page is same-origin, so `sessionStorage` survives the hide→unmount→reopen cycle
within a console tab — a reopened view catches up from its stored mark instead of refetching or
polling. On hosts that predate #1640 the extra fields are ignored and events carry no `seq`;
if you need to support them, keep a poll fallback and disarm it the first time a `seq` arrives.

## Trust & the sandbox split

There are **two different** iframe-sandbox postures. Do not blur them.

- **Plugin rail/right views are isolation-of-convenience, not a security boundary** (ADR 0026 D6).
  The console sets `sandbox="allow-scripts allow-forms allow-same-origin"` on your iframe.
  `allow-same-origin` is required so your page can call its own `/api/` (the bearer handshake works).
  This scopes your CSS/JS from the console — but it is **not** protection against a malicious enabled
  plugin: an enabled plugin already runs **in-process as the agent** (same trust model as plugin
  backends, ADR 0018). Only enable plugins you trust.

- **Untrusted generated content gets `sandbox="allow-scripts"` with NO `allow-same-origin`** — the
  **artifact** pattern (ADR 0026 D6 / ADR 0038). When the *agent generates* HTML/SVG/React
  (`show_artifact`), it's rendered in a nested iframe with **no** same-origin and often via `srcdoc`,
  so it runs but **can't** touch the console, its cookies, or its APIs. This is the one place the
  sandbox is a genuine security line — "code the model emitted," not "code you installed."

The rule of thumb: a **plugin** you installed → `allow-same-origin` (trusted, can call its API);
**generated** content → no `allow-same-origin` / `srcdoc` (untrusted, fully fenced). See the
[security & trust model](../explanation/security-and-trust.md).

## Want React (or any framework)?

Build your UI however you like and **serve the built files** — inline (a single `_PAGE` string), as
static assets your plugin serves, or pull libs from a CDN at runtime (the **artifact** plugin loads
React + Babel from a CDN inside its sandbox). The console just iframes your `path`.

## Reference plugins

- **[`examples/plugins/chat_example`](https://github.com/protoLabsAI/protoAgent/tree/main/examples/plugins/chat_example)** —
  the **gold-standard copy-me view**: a single-page chat panel covering all four rules + the
  handshake + live re-theme + slug-aware routing + a real turn over a gated route. Start here.
- **artifact-plugin** (external — github.com/protoLabsAI/artifact-plugin) — generative UI: the agent
  calls `show_artifact(kind, code)`; renders in a **nested, no-same-origin** sandboxed iframe (the
  untrusted-content posture above). Uses a **custom router prefix** — a good example of RULE 1 with a
  non-default prefix.

Both are pure Python + a served page + (optionally) a bundled `SKILL.md`, fully distributable from a
git URL.

## Lifecycle

- Views appear for **enabled** plugins; disabling one (or a config reload that drops it) removes its
  rail icon and, if you were on it, falls back to Chat.
- Adding a view to an existing plugin needs a **restart** (routes mount once at init) — but the rail
  picks up the declaration from `runtime-status` with **no console rebuild**.

## Fork components (no plugin, no iframe)

If you're a **fork** and want first-party components compiled *into* the console (not sandboxed),
that's the build-time `src/ext/` seam — see ADR 0038 D3. Different from plugins, which are runtime +
sandboxed.

## References

- [Build a plugin view (quickstart)](/how-to/build-a-plugin-view) — the short entry the DS kit header
  points to, plus the kit helper API at a glance.
- [Security & trust model](../explanation/security-and-trust.md) — why plugin UIs are sandboxed
  iframes, and the "installing a plugin runs its code" trust posture.
- ADR 0026 (plugin console surfaces + the bridge + the sandbox split), ADR 0027 (git-URL install),
  ADR 0038 (the two-mode model + why federation was retired), ADR 0039 (the event bus), ADR 0042 (the
  `/agents/<slug>/` fleet proxy), ADR 0045 (the chat slot).
