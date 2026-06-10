# 0026 — Plugin-contributed console surfaces (rail views + tabs)

Status: **Accepted** (sliced; PR1 = thin vertical)

## Context

Plugins already extend the **backend** (tools, subagents, MCP servers, lifecycle
surfaces, FastAPI routers at `/plugins/<id>`) per [ADR 0018](./0018-plugin-surfaces-routes-subagents.md),
and the **Settings** surface (config/secrets → fields under Settings →
Integrations) per [ADR 0019](./0019-plugin-config-settings-secrets.md). The
delegates panel + Discord/Google are the worked precedents.

What a plugin **cannot** do is add its own **console surface** — a left-rail icon
opening a full view (a dashboard, a board, whatever the fork wants), with its own
sub-tabs. The rail and surfaces are hardcoded in `apps/web/src/app/App.tsx`:
`type Surface = "chat" | "activity" | …` (a fixed union), hardcoded `RailButton`s,
`surface === "x" ? <Component/> : null`, and fixed sub-tab unions
(`ActivityTab`, etc.). The console is a compiled Vite SPA; plugins are runtime
Python — so there's no point to inject a React view.

But two of the three expensive seams already exist: a plugin can **serve arbitrary
HTTP** at `/plugins/<id>/…` (`register_router`), and the enabled-plugin list is
already shipped to the frontend via `/api/runtime/status`. This ADR adds the
third: a way for a plugin to **declare a console view**, and for the console to
render a rail icon + host it — without a console rebuild.

This is "ADR 0018 for the frontend."

## Decision

A plugin declares **views** as data in its manifest; the console renders a
**dynamic rail icon per view** and hosts the view as a same-origin **iframe** of a
page the plugin serves. Locked decisions:

### D1 — Approach: iframe-embed (not React federation, not schema-only)

The plugin serves its own UI (any framework) at a route under `/plugins/<id>/…`;
the console renders a rail entry whose panel is `<iframe src="/plugins/<id>/…">`.
- **Why not module federation** (plugins ship React components into the bundle):
  heavy build tooling, version pinning, and it runs plugin JS in the console's own
  module graph — against the "drop in a Python package" ethos.
- **Why not schema-only** (plugin declares data, console renders generic widgets):
  great for native-look dashboards but caps expressiveness; kept as a
  *complementary future option* (D8), not the v1.
- Iframe-embed gives forks **any dashboard they want** with zero console rebuild,
  reusing the existing router seam.

### D2 — Declaration: a manifest `views` block (data only)

```yaml
# protoagent.plugin.yaml
views:
  - id: board                 # unique within the plugin
    label: "Board"            # rail + tab label
    icon: LayoutDashboard     # a lucide icon name (see D4)
    path: /plugins/myplugin/board   # what the iframe loads (plugin-served)
    placement: rail           # "rail" (default — a left-rail surface) | "right"
    tabs:                     # optional sub-nav (view-tabs)
      - { id: open, label: "Open", path: /plugins/myplugin/board?tab=open }
      - { id: done, label: "Done", path: /plugins/myplugin/board?tab=done }
```

**Placement (later addition).** A view's optional `placement` puts it either in the
**left rail** as a full surface (`rail`, default) or in the **right sidebar** as a panel
alongside Notes / Beads / Goals / Schedule (`right`). Both render the same iframe host;
`right` is the substrate for moving Notes itself to a plugin. (No backend change — the
manifest passes view dicts through verbatim; the console filters by `placement`.)

Declared as **pure data** (like `config`/`settings`, ADR 0019) so it's known
without importing the plugin, and surfaced to the frontend via
`/api/runtime/status` (the plugin meta already flows there). The plugin serves
`path` via `register_router` — already supported.

### D3 — Console rail becomes data-driven

`App.tsx`'s hardcoded `Surface` union becomes a **registry**: the fixed core
surfaces **plus** plugin views read from `runtime-status`. A plugin surface is
keyed `plugin:<id>:<viewId>`; selecting it renders a generic `PluginView`
(`<iframe>`), and its `tabs` render the existing `stage-subnav` data-driven.
Core surfaces are unchanged in behavior.

### D4 — Icons: a lucide-name allowlist (+ optional plugin SVG)

`icon` is a **lucide-react icon name** the console maps from a curated allowlist
(broadened to cover dashboards, data, comms, dev, AI, finance, space/fleet, and
security — e.g. `LayoutDashboard`, `BarChart3`, `Rocket`, `Satellite`, `Bot`,
`Coins`, `Workflow`, `Shield`); an unknown/missing name → a default "plugin" glyph.
A plugin may instead point `icon` at an SVG it serves (`/plugins/<id>/icon.svg`)
for a custom mark. Lucide-name is the common path (matches the rest of the rail).

### D5 — Auth: same-origin, token handed in post-load (no token-in-URL)

The console and `/plugins/<id>/…` are the **same origin** (one FastAPI app), so the
iframe inherits the operator-console posture (localhost-default + bearer-when-
exposed, #581/#591) and same-origin cookies/session. When a bearer is configured,
the console hands the iframe its token via `postMessage` **after** load (not a URL
query param — avoids token leakage to logs/history). The plugin page listens for
the handshake and uses the token for its own `/api/plugins/<id>/…` calls.

### D6 — Trust & sandbox

An enabled plugin already runs **in-process as the agent** (ADR 0018 trust model —
"don't enable code you don't trust"). Its view runs in an iframe with
`sandbox="allow-scripts allow-forms allow-same-origin"` (same-origin is required
for it to call its own API). This is isolation-of-convenience (DOM/CSS scoping),
**not** a security boundary against a malicious enabled plugin — same posture as
the backend. Documented as such.

### D7 — Theming bridge

On iframe load the console `postMessage`s the brand tokens (the `@protolabsai/design`
`--pl-*` ground, dark-first) so an embedded view can match the console look. Opt-in
for the plugin page; core views are unaffected.

### D8 — Slices

- **PR1 (this ADR): thin vertical.** Manifest `views` parsing + surface it in
  `runtime-status`; the `hello` plugin gains a `views:` entry serving a demo page;
  the console renders **one** dynamic rail icon + iframe end-to-end. Proves the
  whole loop. (Hardcoded-to-registry refactor minimal: append plugin views after
  core surfaces.)
- **PR2 (shipped): rail registry + PluginView host.** Data-driven plugin rail
  (`plugin:<id>:<viewId>`), the generic `PluginView` iframe host (load/error
  states), stale-surface fallback (disabled/missing → chat). e2e (#620).
- **PR3 (shipped): view-tabs + auth/theming bridge + sandbox + docs.** Declared
  `tabs` → `stage-subnav`; the post-load `postMessage` token + theme handshake
  (`protoagent:init`); sandbox attrs; the `hello` view is the reference receiver;
  `docs/guides/building-react-plugin-views.md` (canonical guide; `chat_example` is the copy-me reference).
- **Later (optional): schema-driven views** (D1) for forks that want a native-look
  dashboard without serving their own page — a separate ADR if pursued.

## Consequences

- Forks get **first-class console real estate** — a rail icon + view (+ tabs) for
  their plugin — by declaring data + serving a page, with **no console rebuild or
  fork of `App.tsx`**. Completes the extensibility story: backend (0018) + settings
  (0019) + **surfaces (0026)**.
- The console takes one structural change (rail: hardcoded → registry); after
  that, new plugin views are config, not code.
- Iframe isolation keeps a plugin's CSS/JS from colliding with the console, at the
  cost of a same-origin auth/theme handshake (D5/D7).
- Trust is unchanged: an enabled plugin's view is as privileged as its backend.

## Alternatives considered

- **Module federation / runtime React** — rejected (D1): build/versioning/trust
  cost, breaks the Python-package ethos.
- **Schema-driven-only** — deferred (D1/D8): native look but limited; a good
  complement later, not the v1.
- **Token via URL query** — rejected (D5): leaks to logs/history; use post-load
  `postMessage`.
