# ADR 0040 — Pluggable utility bar: declarative widgets + data tickers

**Status:** Proposed

## Context

The console shell (ADR 0035 dual-rail / the DS `AppShell`, #144) has a **utility bar** — the
bottom row of the shell grid (`grid-template-rows: 48px minmax(0,1fr) 40px` → topbar / content /
**utility bar**). Today it holds fixed core items: docs + GitHub links and the side-panel toggle.

We want **plugins to contribute to it** — small status widgets and **live data tickers** (a trading
plugin's price, a CI plugin's build state, a queue depth, "5 PRs open ▲"). And — separately — the DS
`AppShell` (#144) never spec'd a slot for this bar; that's the render target.

Plugin *rail views* (ADR 0026) are **iframes**. That's right for a full surface, but **wrong for the
utility bar**: widgets are tiny, numerous, and update continuously — an iframe per ticker is heavy,
slow, visually inconsistent, and can't share the bar's 40px track. The utility bar needs a different,
lighter contribution model.

## Decision

### D1 — `AppShell` gains a `utilityBar` slot (DS, upstream #144)

The DS `AppShell` exposes `utilityBar?: ReactNode`, rendered as the bottom grid track (the `40px`
row). The host fills it. Spec'd upstream against protoContent #144 (the slot we forgot). The console
fills it with **core items + the plugin-widget strip** (D5).

### D2 — Utility-bar widgets are **declarative, rendered natively** — not iframes

A plugin declares widgets as **data** in a manifest `utility` block; the **console renders them
natively** from that spec (icon + text + tone), not via an iframe. This keeps the bar fast,
on-brand (DS tokens), and overflow-managed, and avoids N iframes for N tickers.

> Contrast: ADR 0026 rail **views** = iframe (rich, sandboxed, full surface). ADR 0040 utility
> **widgets** = declarative (tiny, data-driven, native render). Rich interaction = open the view.

### D3 — Three widget kinds

```jsonc
// plugin manifest: utility: [ … ]
{ "kind": "indicator", "id": "build", "icon": "git-branch", "label": "passing",
  "tone": "success", "topic": "ci.status", "placement": "end" }
{ "kind": "ticker",    "id": "btc", "icon": "coins", "topic": "prices.btc",
  "format": "BTC ${value} ${arrow}", "placement": "end" }
{ "kind": "action",    "id": "open", "icon": "rocket", "label": "Deploy",
  "view": "deploy", "placement": "start" }
```

- **`indicator`** — icon + short label + `tone` (StatusDot/Badge). Static, or bus-driven (latest
  event on `topic` updates the label/tone).
- **`ticker`** — a live readout fed by an event-bus topic. `format` is a restricted template over the
  event payload (text only; optional `sparkline` over a numeric series). No code.
- **`action`** — an icon button that opens the plugin's `view` (its iframe surface) or emits an event.

### D4 — Data flows over the event bus (ADR 0039), not polling

Tickers/indicators don't poll. The plugin (or the agent) **publishes** updates on its namespaced topic
`<pluginId>.<topic>`; the console's widget **subscribes** and re-renders on each event. Namespaced +
guarded per ADR 0039 (a plugin publishes only under its own namespace). This decouples the producer
(plugin/agent/external feed) from the widget, and reuses the existing bus + the iframe relay for
sandboxed producers. The agent can drive a ticker too (publish `<id>.<topic>` from a tool).

### D5 — Placement, ordering, a registry

Widgets declare `placement: "start" | "end"` (left cluster vs right cluster, around the existing
`util-spacer`) and optional `order`. A **registry** merges **core anchors** (docs/GitHub/panel-toggle)
with plugin widgets — the same pattern as the context-menu registry (ADR 0036) and the data-driven
rail (ADR 0026 D3). The console assembles the strip and hands it to `AppShell.utilityBar`.

### D6 — Trust & safety (aligns with ADR 0038)

Declarative widgets run **no plugin code** in the host — the console renders icon/text/tone from the
manifest spec + bus data, so they're **safe for untrusted plugins** (ADR 0038's "declarative for
untrusted, iframe-sandbox for rich, build-time `src/ext` for trusted forks"). Guards:
- Bus payloads are **untrusted text** — rendered, never `eval`'d; `format` is a fixed template engine
  (interpolation only), not arbitrary JS.
- Icons via the lucide-name allowlist (+ optional plugin SVG), per ADR 0026 D4.
- Label/value length-clamped; tone from the `Status` enum only.
- An `action` can only open the plugin's own declared view or emit under its own namespace.

### D7 — Overflow

The bar is width-bounded (one 40px row, never wraps). Excess widgets collapse into a **"more"
popover** (DS `Menu`), ordered by `order`. Tickers the user pins stay inline; the rest overflow.

## Consequences

- **Plugins get an ambient presence in the shell** (live status + tickers) without iframe cost — the
  bus becomes the data plane for at-a-glance readouts (a natural fit for the trading/ops/CI forks).
- **The `AppShell.utilityBar` slot** (#144) is the shared render target; the declarative widget +
  registry + bus-subscription logic stays **app-side** (the DS owns the slot + the widget *primitives*
  — `StatusDot`/`Badge`/`Spinner`/`Menu` — not the contribution model).
- New manifest surface (`utility` block) + a console utility-bar registry + a ticker-subscription hook.
- A restricted `format` template engine (interpolation-only) to keep declarative widgets safe.

## Build order (proposed slices)

1. `AppShell.utilityBar` slot upstream (#144) + console fills it with the current core items (no
   behavior change) — the seam.
2. The utility-bar **registry** + `placement` clusters; core items become registrations.
3. Manifest `utility` block + native renderers for `indicator` / `action` (static first).
4. **`ticker`** + the bus-subscription hook + the restricted `format` engine (+ optional sparkline).
5. Overflow popover (DS `Menu`); the devkit + a reference ticker (e.g. the artifact or a demo prices
   plugin).

## References

- ADR 0026 (plugin-contributed surfaces — manifest-as-data, data-driven rail, lucide icons, trust),
  ADR 0035 / #144 (the shell + the `utilityBar` slot), ADR 0038 (the trust model: declarative for
  untrusted), ADR 0039 (the event bus — the widget data plane). DS primitives: `StatusDot`, `Badge`,
  `Spinner`, `Menu`.
