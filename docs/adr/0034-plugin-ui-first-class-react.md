# ADR 0034 — Plugin UI as first-class React (Module Federation)

**Status:** Proposed

## Context

ADR 0026 gave plugins a console surface, but the mechanism is a **same-origin iframe**:
the plugin serves a page, the console embeds it (`apps/web/src/app/App.tsx` — `placement:
"rail" | "right"`). That works, but it's second-class — and, importantly, a **same-origin
iframe buys no real security isolation** (it can reach `window.parent`). So today we pay the
*developer-experience* cost of isolation without the *security* benefit:

- no shared design system (the plugin re-implements or re-imports the theme),
- no shared state — the host's `react-query` cache, auth/bearer, and event stream don't cross
  the frame (everything is `postMessage` or a re-fetch),
- focus/scroll/resize/theming jank at the frame boundary,
- a separate build + bundle per plugin view.

The goal: let plugin authors **ship actual React components** that mount into the console's
own React tree — share the design system, the query cache, the API client — so "drop in a
plugin and it has a real UI" is true. We want **Notes** (currently a native view + native
tools) to become the flagship first-party plugin demonstrating this in the **right panel**.

## Decision

### D1 — Two view families behind one manifest field

Extend the ADR 0026 view manifest with a `ui` kind; the renderer dispatches on it:

- `ui: iframe` (default, unchanged) — same-origin iframe of a plugin-served page. Stays the
  path for **untrusted / third-party** plugins (git-URL installs, ADR 0027).
- `ui: react` — a **federated React remote** mounted directly into the host tree at the
  declared `placement` (`rail` | `right` | `tab`, reusing 0026). For **trusted** plugins.

`placement` and the rail/right/tab wiring from ADR 0026 are unchanged — only *how the body
renders* changes.

### D2 — Module Federation as the runtime loader

Use `@originjs/vite-plugin-federation`. The console (`apps/web`) is the **host**; a `ui: react`
plugin builds a **remote** bundle. At runtime the console reads the manifest, dynamically
imports the remote's exposed module, and mounts its default-exported component at the placement.
This keeps the **git-URL drop-in** promise (ADR 0027) for UI: a plugin ships a pre-built remote;
no host rebuild required to gain its view.

Rejected alternatives: **build-time bundling** (small lift but every fork rebuilds the console
per plugin set — breaks drop-in); **import-maps/ESM** (lighter but thin tooling/ecosystem, more
manual dedupe). Federation is the mature Vite answer and handles shared-singleton dedupe for us.

### D3 — The host shares singletons (non-negotiable)

Two React copies break hooks. The host exposes a fixed **shared** set that remotes consume,
never re-bundle: `react`, `react-dom`, `@tanstack/react-query` (one cache), the router, the
**design system / theme**, and the **API client** (so a plugin's calls carry the host's auth +
hit the same query cache). All pinned as `singleton: true` with the host's version as the
floor.

### D4 — A versioned plugin-UI SDK (`@protoagent/plugin-ui`)

The stable contract a `ui: react` plugin imports — this is the actual product surface, and it
is **versioned** (semver; the host advertises the range it supports):

- the API client + auth/bearer, the shared `QueryClient`,
- theme tokens + the reusable shell pieces (`PanelHeader`, the right-panel host, cards),
- nav/placement registration, the event/SSE stream, and a typed `usePluginConfig()`.

A remote is a thin component that imports from `@protoagent/plugin-ui` and renders — exactly
the ergonomics of writing a view *inside* `apps/web`, but shipped from the plugin repo.

### D5 — Trust model: React = trusted, iframe = the rest

A federated remote runs **with full console privileges** (no sandbox) — that's the point, and
the risk. So `ui: react` is gated:

- **First-party** plugins (in-repo) — trusted, always allowed.
- **Third-party** (git-URL, ADR 0027) — `ui: react` requires an explicit operator **trust
  opt-in** per plugin; absent that, the view falls back to `ui: iframe` (or is hidden).

(If we ever need real isolation for *untrusted* rich UI, the lever is a **cross-origin
sandboxed** iframe — a separate concern from this ADR, which is about first-class trusted UI.)

### D6 — Fail safe, never white-screen

A remote that fails to load (network, version skew, throw) renders a bounded **error card** at
its placement (name + "failed to load" + retry), never a blank console or a crashed host. The
shared-deps range from D3 is enforced; an incompatible remote degrades to the card, and (if
declared) its `ui: iframe` fallback.

### D7 — Notes is the reference port

Port **Notes** from native (built-in view + built-in `notes_*` tools) to a first-party plugin:
its `notes_*` tools move to the plugin via `register_tools` (composing onto the operator MCP
bus for free — ADR 0033 D3), and its UI becomes a `ui: react` remote in the **right panel**.
This both proves the harness end-to-end and completes the lean-core "Notes → plugin" item.

## Consequences

- **Plugins become a real UI ecosystem** — authors write React against a shared SDK, not
  iframe'd islands; the design system + query cache + auth are free.
- **The SDK contract (D4) is now a maintained surface** — versioned, documented, and a source
  of coupling: host upgrades that change shared-dep majors (React) can break remotes, mitigated
  by the range check + error card (D6).
- **A real trust boundary is introduced** (D5) — first-class React is privileged; third-party
  rich UI stays sandboxed via iframe. This is a deliberate, documented escalation.
- **Bigger lift than iframe-Notes** — the harness + SDK + loader is a multi-PR initiative; the
  payoff is every future plugin view, not just Notes.
- **Backward-compatible** — existing `ui: iframe` views (and the default) are untouched; this
  is purely additive on top of ADR 0026.

## Build order (proposed slices)

1. **Federation harness** — `@originjs/vite-plugin-federation` on `apps/web` as host, the
   shared-singleton set (D3), a runtime remote loader keyed off the manifest, and the D6 error
   boundary. A trivial in-repo "hello-react" remote proves mount + shared React.
2. **`@protoagent/plugin-ui` SDK** — extract the API client/auth, `QueryClient`, theme tokens,
   `PanelHeader`/right-panel host, nav registration; version it; the hello remote consumes it.
3. **Trust gate** — manifest `ui: react|iframe`, the third-party trust opt-in (D5), iframe
   fallback; renderer dispatch in `App.tsx`.
4. **Port Notes** — `notes_*` → plugin `register_tools`; the Notes UI → a `ui: react` right
   panel remote; retire the native view. The reference plugin + docs (a "build a React plugin
   view" guide).

## References

- ADR 0026 (plugin console surfaces — rail/right/tab, the iframe host this extends), ADR 0027
  (install plugins from a git URL — the drop-in promise federation must preserve), ADR 0033 D3
  (operator tools as one MCP bus — where the migrated `notes_*` tools land for free).
- External: `@originjs/vite-plugin-federation` (Vite Module Federation), Webpack Module
  Federation "shared singletons" model, React "multiple copies of React" hooks pitfall.
