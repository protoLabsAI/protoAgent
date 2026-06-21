# Component & styling sharing standard — `@protolabsai/ui`

How components and styling are shared between the protoAgent console (`apps/web`) and the published
`@protolabsai/ui` package. This is the contract: follow it when building a console component, deciding
whether something belongs in the design system, or contributing one upstream.

Companion to the [UI component audit](./ui-component-audit.md) (the inventory + wishlist). Coordination
happens on [protoContent #137](https://github.com/protoLabsAI/protoContent/issues/137).

---

## The principle

> **Componentize → prove in the console → extract upstream → adopt back.**

Build a component props-driven, token-only, and app-decoupled in protoAgent first; prove it against the
real console; then lift the stable shape into `@protolabsai/ui`; then consume it back as a dependency.
The console is the proving ground; the package is the shared, versioned result. We never design DS
components in the abstract — they earn their place by working here first.

---

## How styling is shared (the 6 rules)

These are non-negotiable for anything published in `@protolabsai/ui`. They're what make a component
themeable, on-brand, and safe to share across the console + every plugin remote.

1. **Tokens are the single source of truth.** Every value resolves to a `--pl-*` custom property from
   `@protolabsai/design` (`--pl-color-*`, `--pl-space-*`, `--pl-radius`, `--pl-font-*`, `--pl-motion-*`,
   `--pl-border-width`). No hardcoded hex, px-for-spacing, or font literals. A brand change happens in
   the tokens, once, and every component re-renders on-brand for free.

2. **className-only over tokens. No Tailwind, no CSS-in-JS.** Components render `pl-*` classes; the
   styles live in the package's `styles.css`. The console imports `@protolabsai/ui/styles.css` once.
   (The console's Tailwind from ADR 0037 is for *app* markup only — it never styles DS components.)

   **Import from the domain subpath, not a root barrel** (0.8.0+ — the root `export *` was removed):
   ```ts
   import { Button, Badge } from "@protolabsai/ui/primitives";
   import { Dialog, Drawer, ConfirmDialog, ToastProvider, useToast, Tooltip } from "@protolabsai/ui/overlays";
   import { Tabs, PanelHeader } from "@protolabsai/ui/navigation";
   import { Field, Input, Select, Textarea, Switch, Checkbox } from "@protolabsai/ui/forms";
   import { Table, StatusDot, Spinner, ScrollArea, Skeleton } from "@protolabsai/ui/data";
   import { Menu, MenuItem, MenuSeparator } from "@protolabsai/ui/menu";
   import { AppShell, SurfaceRail, MobileNav } from "@protolabsai/ui/app-shell";
   import "@protolabsai/ui/styles.css"; // still one CSS import
   ```
   Modules: `primitives` · `layout` · `marketing` · `navigation` · `forms` · `overlays` · `data` ·
   `menu` · `app-shell`. Internals stay private (no deep imports past a subpath).

3. **Class naming: BEM-ish `pl-` namespace.** `pl-x` (root), `pl-x__part` (element), `pl-x--variant`
   (modifier), `is-active`/`is-open` (state). Mirrors the existing package (`pl-panel-header__title`,
   `pl-btn--ghost`, `pl-rail__badge`).

4. **Radix only where behavior is genuinely hard.** Interactive primitives needing focus management,
   keyboard nav, or collision-aware positioning (`Menu` today; `Popover`/`Combobox`/`Select` later)
   are built on **unstyled Radix** + our token CSS — Radix owns a11y, we own looks. Everything else is
   plain className-only. Don't reach for Radix for a button or a card.

5. **Icon-agnostic.** The package depends on **no icon library**. Icons are passed in as `ReactNode`
   props (e.g. `RailItem.icon`, `MobileNav`'s `moreIcon` with an inline-SVG default). The console
   passes its `lucide-react` glyphs in; the DS never imports them.

6. **a11y belongs to the primitive.** Shared components carry their own roles, labels, focus rings, and
   reduced-motion handling (`@media (prefers-reduced-motion)`), so consumers get it for free and can't
   forget it. Radix covers the hard cases; we add `:focus-visible` rings, `aria-current`, etc.

---

## How components are shared (the boundary)

**A component belongs in `@protolabsai/ui` when it is:**
- **Controlled + presentation-only** — state comes in via props, changes go out via callbacks. No
  internal data fetching, no business logic, no persistence.
- **App-decoupled** — no protoAgent-specific types, routes, stores, or `data-testid`s.
- **Reused or reusable** — used across surfaces, or a primitive any app/plugin would want.

**Acceptable native patterns (don't force a DS component):**
- **Simple tooltips** — a native `title=` attribute is fine for plain hover hints; reserve DS `Tooltip`
  for rich/interactive tooltip content. (We did not convert ~89 native `title=` hints.)
- **Structured alert composites** — an alert with an icon + an action button (a retry card, a restart
  banner) is *not* a `Callout` (which is a plain note block, no icon/action slot). Swapping it is a
  redesign, not an adoption — keep it app-side (or redesign in a reviewed slice). Bare-text notices
  *do* map to `Callout`.
- **`ref`-needing form controls** — the DS form primitives aren't `forwardRef`; an `<input>`/`<textarea>`
  that needs a `ref` (autosize, focus) stays raw until the DS forwards refs.
- **External-label form controls** — DS `Checkbox`/`Switch` take no `id`/`...rest`, so a control
  associated via `<label htmlFor>` stays raw (tracked: protoContent #155).

**App-domain concerns stay in the console**, even for a shared component:
- **Persistence & registries.** `AppShell` is controlled; the console owns rail order, panel widths,
  the surface registry, and their `localStorage` persistence (Zustand). The DS owns layout + the
  resize/mobile mechanics.
- **Keying & dispatch.** The context-menu **registry** + `ContextType` keying + open-state store are
  app logic; only the rendered `Menu` primitive is shared (the console wires `onContextMenu` →
  `menuRef.open({x,y})`).
- **App-specific rendering.** The markdown renderer, chat tool-renderers, the slash-command menu, the
  setup wizard flow, the workflow-builder canvas, and domain rows (activity/inbox/tasks) are *not* DS
  candidates — they stay local.

---

## The contribution flow (gap → shipped → adopted)

1. **Find the gap** in the console (a bespoke widget that should be shared, or a missing primitive).
   Confirm it isn't already in the latest `@protolabsai/ui` (check the installed `src/index.tsx`, not
   memory — it moves fast).
2. **File a `design-system` issue** on protoContent describing the gap, the proposed API, and a link to
   the proven console implementation to hand over. Children link to the **#137** umbrella.
3. **Land it** — the UI team builds it (or accepts a PR), following the 6 styling rules above.
4. **Version via Changesets.** Every change adds a `.changeset/<slug>.md` (`minor` for additive,
   `patch` for fixes); the `changeset-release/main` bot PR bumps the version + changelog + publishes.
   **Never hand-edit the package `version`** — that's the bot's job.
5. **Adopt back** in the console: bump `^x.y.0`, swap the local primitive for the DS one, **retire the
   local component + its bespoke CSS** (this is how `theme.css` slims — adoption deletes CSS as a side
   effect, no separate decomposition needed).
6. **Each console adoption is a HELD draft PR** per the UI local-test gate — CI/Playwright can't judge
   UX (scroll feel, dialog look, spacing), so a human verifies at `:7901` before merge.

---

## Versioning & consumption

- **Semver, additive-first.** New component or new optional prop = `minor`; bug/parity fix = `patch`.
  Breaking a published API is avoided — extend (optional props) instead.
- **Consume with `^x.y.0`** so patch/minor parity fixes flow in.
- **Backwards-compatible by default.** New `TabItem.icon?`/`.badge?`, `Button` `variant`/`size`/`icon`
  were all additive — existing call sites kept working untouched. Hold that bar.

---

## Status (2026-06-09)

The entire original wishlist shipped across three releases — `0.5.0` (Menu, PanelHeader, Tabs slots),
`0.6.0` (Button variants, Skeleton, ScrollArea hardening), `0.7.0` (SurfaceRail, MobileNav, AppShell).
The console adoption sweep (retiring local primitives + their CSS) is in progress; see the audit doc
for the per-component plan.
