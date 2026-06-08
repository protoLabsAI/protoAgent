# ADR 0037 — Design-system foundation: Tailwind + `@protolabsai/design` + shadcn/Radix

**Status:** Proposed

## Context

The console (`apps/web`) is hand-rolled CSS (`theme.css`) with bespoke components. It works, but:
it's not on the **protoLabs design system**, components aren't reusable/themeable in a standard
way, and there's no accessible primitive layer (the ADR 0036 context menu would have to re-solve
focus/keyboard a11y from scratch). We want to move the console onto **protoContent's design
system** and adopt **Radix + shadcn** as the component foundation — so components are
componentized, themable, accessible, and shareable (incl. to plugin remotes, ADR 0034).

**What protoContent gives us.** `@protolabsai/design@^0.3.0` (already a root dependency, used by
the docs theme) is a **tokens + Tailwind-preset + brand-assets** package — *not* components:

- `@protolabsai/design/tailwind` — a **Tailwind preset** mapping brand tokens into the theme
  (`bg-bg`, `text-fg-muted`, `brand`/`brand-lavender`, `font-mono`, `shadow-glow`,
  `bg-brand-gradient`, …), generated from the locked token source so it never drifts.
- `@protolabsai/design/css/tokens` — the CSS custom properties (`--pl-color-*`, `--pl-font-*`,
  `--pl-motion-*`, …); `…/css/base`, `…/assets/*`.

So **themable components = shadcn/Radix components styled with Tailwind utilities that resolve to
the brand tokens.** This ADR adopts that stack and supersedes the vague "DS/theming pass" of ADR
0035 S5.

## Decision

### D1 — Tailwind in `apps/web`, fed by the design preset

Add Tailwind to the console with `presets: [require("@protolabsai/design/tailwind")]`, and import
`@protolabsai/design/css/tokens` (the `--pl-*` vars) + base. Tailwind **coexists** with the
existing `theme.css` during migration — scope/disable preflight so it doesn't clobber current
styles; no big-bang rewrite.

### D2 — shadcn + Radix as the component layer

Adopt **shadcn** (Radix primitives + Tailwind) as the component foundation, generated into
`apps/web/src/components/ui/`. Radix gives **accessibility for free** (focus management, keyboard
nav, dismissal) — which directly resolves the ADR 0036 renderer-a11y gap Quinn flagged. shadcn
components are *owned source* (copied in, not a black-box dep), so we restyle them with our tokens.

### D3 — One theme, the `--pl-*` tokens

The brand tokens are the **single source of truth**. Bridge shadcn's theming (its
`--background`/`--foreground`/`--primary`/… CSS vars) onto `--pl-color-*`, so shadcn components,
new Tailwind markup, and the legacy `theme.css` all render the *same* dark-first brand theme
(brand violet `#9b87f2` / lavender). Theme changes happen in tokens, once.

### D4 — Incremental migration, not a rewrite

New components are shadcn/Radix + Tailwind. Existing surfaces migrate **opportunistically** (when
we touch them), not all at once — `theme.css` and Tailwind live side by side until the legacy CSS
is whittled down. Each migration is its own small, locally-tested PR (the UI local-test gate
applies).

### D5 — Plugin remotes get the same components + theme

The `@protoagent/plugin-ui` SDK (ADR 0034) re-exports the shadcn component set + exposes the
Tailwind preset / tokens, and Module Federation **shares** them — so a `ui: react` plugin remote
renders with the *same* accessible, on-brand components as the host, for free. (This is partly
*why* we standardize: a shared component contract for plugins.)

### D6 — Supersedes / flips

- **Supersedes ADR 0035 S5** ("design-system + theming pass") — that slice *is* this foundation.
- **Flips ADR 0036 D5** — the context menu is **not** a lean custom renderer; it's built on
  shadcn's Radix `DropdownMenu` (imperative open-at-coords) themed by the tokens. The 0036 registry
  / `ContextType` / store design is unchanged; only the renderer's implementation changes.

## Consequences

- **On-brand, accessible, reusable components** — and a real shared contract for plugin UI (D5).
- **A migration debt window** — Tailwind + `theme.css` coexist for a while; we accept two styling
  systems mid-flight, with a clear direction to converge on tokens.
- **New build deps** (tailwindcss, the shadcn deps: class-variance-authority, clsx, tailwind-merge,
  the `@radix-ui/*` primitives) + Tailwind in the Vite build. Bundle managed via tree-shaking +
  shadcn's copy-only-what-you-use model.
- **`@protolabsai/design` becomes a console dependency** (already a repo dep) — fork/version
  coupling, but it's the locked brand source so that's intended.

## Build order (proposed slices)

1. **Foundation** — Tailwind + the `@protolabsai/design` preset + token CSS wired into `apps/web`
   (coexisting with `theme.css`); shadcn initialized; the token bridge (D3); a **pilot component**
   (e.g. Button) themed + swapped in one place to prove the stack end-to-end.
2. **Context menu on shadcn/Radix** — ADR 0036 slice 1's renderer built on shadcn `DropdownMenu`
   (a11y for free) + the rail-surface menu.
3. **Incremental surface migration** — convert high-traffic components (PanelHeader, buttons,
   dialogs, the rails' chrome) to shadcn + tokens; shrink `theme.css`.
4. **Plugin-ui exposure** — the SDK (ADR 0034 S2) re-exports the component set + preset/tokens for
   federated remotes.

## References

- `@protolabsai/design` (tokens + `…/tailwind` preset + `…/css/tokens` + assets; brand violet
  `#9b87f2`, see [[brand-assets-source]] in memory). shadcn/ui (owned-source Radix + Tailwind
  components). ADR 0034 (plugin-ui SDK shares these to remotes), ADR 0035 (this is its S5),
  ADR 0036 (context menu renderer now uses shadcn Radix `DropdownMenu`).

## D7 — `@protolabsai/ui` is the component source; AppShell convergence

protoContent is actively building **`@protolabsai/ui`** (alongside `@protolabsai/design`) — Dialog,
Drawer, Toast, Tooltip, Table, Input/Select/Switch/Checkbox, etc. (→ 0.4.0). So:

- Our **locally-built shadcn components** (`Button`, `dropdown-menu`, `SurfaceRail`) are **interim
  scaffolding**. When `@protolabsai/ui` lands we **adopt its components and retire the locals**
  (the `dropdown-menu` stays until it ships a menu/dropdown — not in the first batch).
- **Principle: componentize + prove here, extract later.** Build reusable, props-driven,
  token-only components in protoAgent first (no app-specific coupling), prove them against the real
  console, then lift the stable ones into `@protolabsai/ui`. `SurfaceRail` is built this way.
- **AppShell convergence (coordination):** protoContent deferred its **AppShell** (icon-rail +
  3-column + resizable panel) to "spec against both dashboards" — that **is** our dual-rail layout
  (ADR 0035: `railOrder`, the resize handle, mobile shell). Feed our proven design into their
  AppShell so the two converge instead of diverging.
