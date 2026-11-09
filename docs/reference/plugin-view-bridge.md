# Plugin view bridge (wire protocol)

A console view is a **sandboxed iframe** ([ADR 0026](/adr/0026-plugin-contributed-console-surfaces)),
so `postMessage` is the only channel between your page and the console. This page is the wire
contract: every message, its direction, its payload, and the guarantees around it.

Most views should use the **plugin kit** (`/_ds/plugin-kit.js`) rather than these messages directly —
start with [Use the kit helpers](#use-the-kit-helpers) below, and see
[Building a plugin view](/guides/building-react-plugin-views). Read the wire protocol when you need to
know what the kit is doing, implement a capability it doesn't wrap, or debug a view that isn't
receiving what you expect.

::: tip Origin
The console posts to the iframe's **resolved origin**, and ignores any message whose `source` isn't
this iframe's `contentWindow` **or whose origin no longer matches**. A view served from an absolute
URL escapes that origin and the handshake silently never completes — which is why `views[].path` must be a same-origin relative path
(the manifest parser warns about this; see [`views`](/reference/plugin-manifest#field-views)).
:::

## Use the kit helpers {#use-the-kit-helpers}

Reach for `/_ds/plugin-kit.js` before the raw messages below: the kit owns the parts a view used
to get wrong by hand — topic naming, the high-water mark on remount, and filtering out the echo of
its own publishes ([#4805](https://github.com/protoLabsAI/protoAgent/issues/4805)). Load it with the
dynamic-import pattern (the slug-aware base is only known at runtime), then call the bus helpers:

```js
const kit = await import(BASE + "/_ds/plugin-kit.js");

// Listen. `patterns` is a string or string[] (`*` is the wildcard); the handler runs for each
// matching frame as { name, data, seq }. Returns an unsubscribe function.
const stop = kit.subscribe(["delegation.#"], (ev) => render(ev.name, ev.data), { background: true });

// Talk back. The topic is forced into your plugin's namespace (see below).
kit.publish("status", { phase: "done" });

// Every frame, raw — including your own echoes. A low-level escape hatch under subscribe.
const off = kit.onEvent((frame) => console.debug(frame));
```

- **Topic qualification.** `publish` qualifies your topic with your plugin's id unless it is already
  prefixed — the same rule as the host's `registry.emit`
  ([#4801](https://github.com/protoLabsAI/protoAgent/issues/4801)). You name `status`; the bus sees
  `<pluginId>.status`. A topic already under your id keeps all its segments.
- **`since` resume — automatic.** The kit records the `seq` of every frame it delivers (per view, in
  `sessionStorage`) and, when a view remounts, resubscribes with that mark as `since` so the host
  replays only what you missed — you don't track a high-water mark yourself. Pass an explicit
  `{ since }` to override it; `background: true` keeps the view mounted (and receiving) while another
  surface is on screen.
- **Echo filtering.** Each `publish` is stamped with this view's identity, and `subscribe` drops the
  host's echo of your own publishes — so a view that both publishes and subscribes to a topic doesn't
  react to itself. `onEvent` is the exception: it taps *every* frame, echoes included.
- **Coalescing.** `publish(topic, data, { coalesceMs })` sends only the last value for a topic within
  that window (trailing edge) — for high-rate state where only the latest matters. (The host also
  coalesces per topic over 100 ms and rate-limits publishes; see
  [the wire protocol](#events-subscribe-event-publish).)

`getState` / `setState` persist a small blob of per-view state on the host (`kit.getState()` /
`kit.setState(obj)`). They need **host support that a follow-up adds**; until then they resolve to
`undefined` without throwing, so a view can call them speculatively.

The handshake, theming, and authed-fetch helpers (`initPluginView`, `apiFetch`, …) are covered under
[The kit](#the-kit).

## Messages at a glance

| Message | Direction | Payload |
|---|---|---|
| `protoagent:restart-request` | page → host | *(none)* — open the host restart confirmation |
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
| `protoagent:code:open` | page → host | `{ project: string, path: string, line?: number, end_line?: number, note?: string, open_in?: "pane" \| "editor" }` |
| `protoagent:chat:compose` | page → host | `{ text: string }` — plain text, ≤ 4 KB; **drafts**, never sends |
| `protoagent:run:open` | page → host | `{ run_id: string }` — opens the run inspector drawer on that run |

Plugin pages may send `{type: "protoagent:restart-request"}` to open the host's restart
confirmation. The host validates the frame source and origin, checks active work, and
waits for idle before restarting. The message never grants consent on the operator's behalf.

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

## Events — `subscribe` / `event` / `publish` {#events-subscribe-event-publish}

Iframe publishes are coalesced per topic over 100 ms (last value wins), then limited
per view to a burst of 20 events with 10 tokens refilled per second. At most 256
distinct topics wait in the client queue. Coalesced and over-limit events are
counted and reported only in the developer console; there is no UI notification.
The console obtains a plugin-bound token from `POST /api/events/publish-token`
(`{plugin_id}`) and sends it in `X-Plugin-View-Token` on each publish. The server
requires the topic to start with `<plugin_id>.` (403 otherwise) and independently
applies the same 20 / 10-per-second bucket per token (429 when exhausted).
Tokens expire after 24 hours, are renewed by the console after 12 hours, and are
held in a bounded, process-local store (4096 active tokens; minting returns 429
when full). Restart invalidates tokens. Both endpoints retain the operator API
bearer gate. Without a view token, existing operator/CLI publishing is unchanged,
including the namespaced-topic, no-wildcards and 64 KB payload checks. These
capabilities constrain token-bearing requests; they do not isolate a page that
already holds the operator bearer from minting tokens or publishing without one.


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
- **A `registry.navigate()` that mounts you also catches you up.** "Publish, then navigate" is the
  usual shape of a tool that drives its view. If the view is already open, the navigate leaves the
  iframe alone and the event arrives live. If another surface holds its dock, the navigate mounts a
  fresh page *after* the event crossed the bus. The host answers that page's first `subscribe` as
  if it had passed a `since`, replaying your plugin's retained events from just before the navigate
  (never further back than what your previous page was offered, nor more than a few seconds). A page
  that sends its own `since` still wins.
  ([#4283](https://github.com/protoLabsAI/protoAgent/issues/4283))
- **`background: true` keeps you mounted while hidden.** Normally only the *visible* plugin's iframe
  is mounted, so a hidden view receives nothing. Only an explicit boolean changes the mount policy —
  omitting the field leaves it alone.
- **`publish` is namespace-forced.** The host forces your topic into your own plugin's namespace
  before it reaches the bus — a page can only ever publish under its own plugin's id. The rule
  matches Python `registry.emit`: **prefix `<pluginId>.` only when it's missing.** A topic already
  equal to `<pluginId>`, or already starting with `<pluginId>.`, is sent unchanged — so a dotted
  topic keeps every segment. Any other topic (including one namespaced to a *different* plugin) is
  prefixed, so it still lands under your id, never the other plugin's.

  | Your topic (plugin `cadview`) | Published on the bus |
  | --- | --- |
  | `view.status` | `cadview.view.status` |
  | `cadview.view.status` | `cadview.view.status` (unchanged) |
  | `cadview` | `cadview` (unchanged) |
  | `status` | `cadview.status` |
  | `other.x` | `cadview.other.x` (forced into *your* id, never `other`) |

  ([#4801](https://github.com/protoLabsAI/protoAgent/issues/4801))

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
  is *your* local one, not the namespaced one. Configure pages add the stable
  `.settings.<tabId>` segment before your id so their bindings cannot replace a simultaneously
  mounted rail/background view from the same plugin.
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

Set `editable: true` when focus is in one of your own text fields — the host honours it, so a
chord that hasn't opted into `allowInInput` doesn't fire out from under someone typing in your
search box. Absent means `false`. (Chords that *are* marked `allowInInput` — the ⌘⇧K palette
among them — still fire; see [ADR 0063](/adr/0063-keybinding-system).)

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

## Show code — `protoagent:code:open` {#show-code}

A page asks the console to show a range of a managed project's code — the Artifact panel's
code-linked diagrams use it. The host accepts it only from the view's own iframe at the page's
origin, and refuses a `path` the fs fence would (absolute, `~`, `..`). It then opens the **code
pane** (ADR 0112) at `line`–`end_line` with `note` as its banner when that toolset is on — beside
the view, never swapping it out — else the operator's external editor (Settings ▸ Chat ▸ Open
files in), else it copies `project/path:line` and says so in a toast. The pane's own routes still
apply the fence and the secret-path deny list, so this can't show anything `show_code` couldn't.

```js
parent.postMessage({ type: "protoagent:code:open", project: "app", path: "src/server.ts",
  line: 23, end_line: 29, note: "the bearer check" }, hostOrigin);
```

### Choosing the destination — `open_in`

Without `open_in` the operator's preference decides, as above. A page that offers two explicit
actions — the chat convention, where a file link's click opens the pane and ⌘/Ctrl-click opens the
editor — names the destination:

| `open_in` | Tries, in order | Toast when it lands elsewhere |
|---|---|---|
| *(absent)* | pane (unless Open files in = editor) → editor → host editor → copy | only on a copy |
| `"pane"` | code pane → external editor → host editor → copy | the pane is off, and how to turn it on (Tools ▸ Filesystem ▸ Shell & filesystem tools ▸ Code pane) |
| `"editor"` | external editor → host editor → code pane → copy | why there was no editor link (none set in Settings ▸ Chat ▸ Open files in, or no filesystem shared with the agent) |

"External editor" is the console's editor link (Settings ▸ Chat ▸ Open files in: `zed://`,
`vscode://`, `cursor://`, opened on the operator's machine). "Host editor" is the agent's
`filesystem.editor_command` (the one `open_in_editor` uses), run by the server through
`POST /api/fs/open-in-editor` — tried only when `/api/runtime/status`
`editor_command_configured` is true, and refused (`not_local`) unless the console runs on the
server's own machine, so it never opens a window on a screen nobody is looking at (a LAN
browser, a remote fleet member, Docker).

Any other value is ignored (the default routing), so a page can send it unconditionally.

```js
// "View" and "Open in editor" buttons on the same target
const show = (open_in) => parent.postMessage({ type: "protoagent:code:open",
  project: "app", path: "src/server.ts", line: 23, open_in }, hostOrigin);
```

**Knowing what's available.** Whether the code pane is on is the agent's toolset state, which
`GET /api/runtime/status` already reports as `code_pane.enabled` — `kit.apiFetch` it to hide or
relabel a "View" action (it's hot-reloadable; re-read it if your view stays mounted). The same
status reports `editor_command_configured` (a host editor exists; never the command). The console's
own editor choice is a per-browser preference and is **not** exposed to pages: offer "Open in
editor" unconditionally and let the fallback and toast cover an operator without one.

A page that renders untrusted content (a sandboxed frame of its own) should forward only targets
it validated — never a path the untrusted frame supplied.

## Draft a chat message — `protoagent:chat:compose` {#chat-compose}

"Ask the agent about this" from a view. The host puts `text` at the **front of the visible chat
composer's draft** (anything the operator already typed stays, after a blank line), raises the chat
surface if it was hidden or its dock collapsed (opening a session if there is none), focuses the
textarea, and toasts **"Drafted in chat — From ‹view label› (‹plugin id›)"** so the operator sees
where the text came from. The operator reviews it and presses Enter. It is the same draft seam the
command palette's skill rows use.

```js
parent.postMessage({ type: "protoagent:chat:compose",
  text: "What does the Login button in src/auth/LoginForm.tsx do?" }, hostOrigin);
```

The contract, and what the host enforces:

- **Draft only — there is no auto-send.** A `send` field is ignored. Gating a send on "a user
  gesture inside the view" isn't enforceable from the host: transient user activation is a shared,
  seconds-long window across the frame tree that proves the operator clicked *something* in the
  view, not that they agreed to send *this* text. The artifact shell's `protoArtifact:send` does
  post, but only through a host-owned confirm chip; for a view, the composer's own Enter is that
  confirmation.
- **Plain text, ≤ 4096 UTF-8 bytes.** Control characters (except tab/newline) are stripped and
  `\r\n` normalized; blank text is ignored. An over-cap payload is **dropped, not truncated** —
  the operator never sends a silently clipped prompt.
- **Always the chat on screen.** There is no session or agent field; anything like one is
  ignored. A view can't draft into a background tab or another agent.
- **One per second per view.** Each compose raises chat and takes focus, so a looping view is
  throttled.
- **Same trust as every other message:** only from this view's own iframe at its page's origin.
  If no composer appears (a `slot: "chat"` plugin or a fork surface owns the chat slot), nothing
  is drafted and the host shows a warning toast instead.

A page that renders untrusted content should build the text itself (e.g. from the element it
selected), never forward a string an embedded frame supplied.

## Open a run — `protoagent:run:open` {#run-open}

A page asks the console to show the operator one run in the run inspector drawer, the same
drawer the chat's **View run** opens. `run_id` is any id the `GET /api/runs/{id}` route resolves:
a run's root id, the uuid of any edge inside it, a background job id, a workflow run id or a
coder task id. The host accepts it only from the view's own iframe at the page's origin, and only
an id of at most 200 characters with no whitespace or control characters. Opening it is
read-only and the operator closes the drawer as they would any other. The org chart uses it for
the **View run** buttons on its drawer's history rows and live cards.

```js
parent.postMessage({ type: "protoagent:run:open", run_id: edge.edge_uuid }, hostOrigin);
```

Only send ids from this agent's own ledger. A peer's run lives in the peer's ledger, so this
console would answer that it has no run under that id.

## Deep links into a view — `?plugin=…&vp=…` {#deep-links}

Not a message: a console URL that opens your view on load and hands your page query data, for a
QR label, a notification or an email. Mint it server-side with
[`sdk.view_url(plugin_id, view_id, params)`](/reference/plugin-sdk-api#sdk-view-url):

```
https://hub.example.ts.net/app/agent/<agent>/?plugin=inventory&view=view&vp=bin%3DB07
```

| Key | Meaning |
|---|---|
| `plugin` | your plugin id (required) |
| `view` | the view id; omitted = your plugin's first navigable view (the same rule as `ui.navigate`) |
| `vp` | a URL-encoded query string, at most 2048 characters |

On load the console resolves the view, opens it (desktop dock or the mobile shell), appends `vp`
to your view's iframe `src` (after any view-tab query), and strips the three keys from the address
bar. So your page reads its params the ordinary way, after the usual `init` handshake:

```js
const bin = new URLSearchParams(location.search).get("bin");
```

The params stay on the frame for the rest of that page load. The frame path always comes from the
view you declared; a link cannot point the frame anywhere else. `vp` is re-encoded, so it is only
ever query data. A link naming an unknown plugin or view, or a malformed id, leaves the normal
console with a toast saying why. Treat every param as untrusted input: anyone can type a URL.

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
| `subscribe(patterns, handler, { since?, background? })` | Subscribe to one or more topic patterns (`*` wildcard); `handler({ name, data, seq })` runs per matching frame, own publishes filtered. Resumes from the stored `seq` on remount. Returns an unsubscribe function. See [Use the kit helpers](#use-the-kit-helpers). |
| `publish(name, data, { coalesceMs? })` | Publish on the bus; the topic is qualified with your plugin id unless already prefixed. `coalesceMs` sends only the last value per topic in the window. |
| `onEvent(handler)` | Raw tap on every forwarded frame (`{ type, seq, name, data, source }`), echoes included — the escape hatch beneath `subscribe`. Returns a remover. |
| `getState()` / `setState(state)` | Read/persist a small blob of per-view state kept on the host. **Needs host support a follow-up adds**; resolves to `undefined` (no throw) until then. |

Related: [Building a plugin view](/guides/building-react-plugin-views) ·
[Plugin manifest ▸ `views`](/reference/plugin-manifest#field-views) ·
[Plugin registry API](/reference/plugin-registry-api)
