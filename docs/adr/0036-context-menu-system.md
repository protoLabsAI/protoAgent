# ADR 0036 — A unified context-menu system (+ plugin-contributed items)

**Status:** Accepted (shipped)

## Context

We keep reaching for per-feature affordances to expose actions — and they don't scale. The
ADR 0035 "move surface to the other rail" started as hover buttons on each rail icon; they
broke the rail layout and added visual noise. The real need is broader: a **right-click context
menu** as a first-class, app-wide primitive — tight, consistent, and **extensible, including by
plugins** (a plugin should be able to add items to an existing menu or define its own).

**Reference (studied):** `rabbit-hole.io` (`~/dev/rabbit-hole.io`) ships exactly this — a
Zustand-backed context-menu module with a **registry keyed by a `ContextType`** string,
`ContextMenuRegistration`s (with optional route + priority), a single imperative
`openContextMenu(type, x, y, ctx)` + one renderer, and menu items as `{ id, label, icon, action,
disabled, visible, variant, shortcut }`. It uses Radix's *DropdownMenu* (not ContextMenu) so the
menu opens at arbitrary coordinates without wrapping every target. We adopt that shape.

## Decision

### D1 — One imperative, state-driven menu

A single menu, driven by store state `{ open, type, x, y, context }`. Any element opens it from
an `onContextMenu` handler via `openContextMenu(type, e, ctx)` (we `preventDefault` the native
menu). One `<ContextMenuRenderer>` mounted at the app root renders the active menu at the
cursor. Imperative open-at-coords (not wrap-each-target) so *anything* can be a trigger.

### D2 — A registry keyed by `ContextType`

`registerContextMenu({ type, items, priority? })` where `type` is an open string
(`"rail-surface"`, `"chat-message"`, `"note"`, `"bead"`, `"background"`, … + plugin types) and
`items` is a `MenuItem[]` or `(ctx) => MenuItem[]`. On open, the registry **merges all
registrations for that type**, priority-sorted and **deduped by item id**, into the rendered menu.
This is the extension point: core features and plugins register independently; the menu for a
type is the union.

### D3 — Item shape

```ts
type MenuItem =
  | { id; label: string | ((ctx) => string); icon?; run: (ctx, helpers) => void | Promise<void>;
      disabled?: boolean | ((ctx) => boolean); visible?: boolean | ((ctx) => boolean);
      danger?: boolean; shortcut?: string; submenu?: MenuItem[] }
  | { id; divider: true }
  | { id; section: string; items: MenuItem[] };
```

`helpers` = `{ close, toast, navigate, … }`. `label`/`disabled`/`visible` may be functions of the
right-clicked `context`, so one registration adapts per target.

### D4 — Plugins contribute items (the point)

The **`@protoagent/plugin-ui` SDK** (ADR 0034) re-exports `registerContextMenu`, so a first-class
React plugin adds items to a **core** menu type (e.g. an extra action on `"bead"`) **or** defines
its **own** type for its surfaces. Items run with console privileges, so this rides the **same
trust gate as `ui: react`** (ADR 0034 D5 — trusted/first-party; untrusted third-party doesn't get
in-process menu items). A manifest-declared *static* item path (label + a tool/route to invoke)
is a later, lower-trust option for iframe plugins.

### D5 — Renderer → shadcn Radix `DropdownMenu` (superseded by ADR 0037)

> **Updated by [ADR 0037](./0037-design-system-foundation.md).** We adopt Radix + shadcn as the
> component foundation, so the renderer is shadcn's Radix `DropdownMenu` (imperative open-at-coords)
> themed by the `@protolabsai/design` tokens — accessibility (focus, keyboard, dismissal) for free,
> closing the a11y gap Quinn flagged. The registry / `ContextType` / store design (D1–D4) is
> **unchanged**; only the renderer implementation. (The original "lean custom renderer, no Radix"
> intent is reversed.)

### D6 — First customers

- **`rail-surface`** (ADR 0035): right-click a rail icon → **Move to other rail** (replacing the
  removed hover buttons), Collapse, and later **Pin to mobile quick-bar** (0035 S4).
- Then **`chat-message`** (copy / retry / cite), **`note`**, **`bead`** — replacing today's
  ad-hoc inline buttons with a consistent menu.

## Consequences

- **One primitive, many menus** — consistent right-click behavior; new actions are a registration,
  not bespoke UI. Kills layout-noise affordances like the rail hover buttons.
- **A real plugin extension surface** — plugins shape the console's menus, not just add views.
- **A maintained contract** (`MenuItem`, `ContextType`, the registry API) — versioned with the
  plugin-ui SDK.
- **Trust boundary** — in-process menu items are privileged; gated like `ui: react` (D4).
- **Discoverability caveat** — right-click is less discoverable on touch; the mobile shell (0035
  S4) keeps primary actions reachable without it (long-press can map to the same menu later).

## Build order (proposed slices)

1. **Core** — store state + `registerContextMenu` registry + `<ContextMenuRenderer>` (custom,
   lean) + an `openContextMenu` helper. Wire the **`rail-surface`** menu (Move to other rail —
   uses the `railOf`/`moveSurface` foundation already in the store from ADR 0035 S3).
2. **SDK export** — `registerContextMenu` from `@protoagent/plugin-ui` (depends on ADR 0034 S2)
   under the trust gate (0034 S3); a reference plugin item.
3. **More core menus** — `chat-message`, `note`, `bead` (retire their ad-hoc buttons).
4. *(optional)* manifest-declared static items for iframe/untrusted plugins.

## Addendum (2026-06-26) — rail-surface management actions + the `hidden` bucket

The `rail-surface` menu grew two management actions beyond move/reorder:

- **Hide** — moves the surface into a new `railOrder.hidden` bucket (uiStore): a surface is now on
  exactly one dock *or* hidden. "Hidden" = *enabled-but-not-shown* — it declutters the rails without
  disabling the plugin (previously the only way to remove a plugin view from a rail). `railSurfaces()`
  renders only the dock arrays, so a hidden id has no rail icon; its safety-net append counts
  `hidden` as "placed" so it never re-adds one. Both reconcilers (`reconcilePluginViews`,
  `reconcileCoreSurfaces`) treat `hidden` as placed, so a reload never resurrects a hidden surface;
  `reconcilePluginViews` prunes a hidden id only when its plugin is uninstalled. **Chat is never
  hidden** (it mounts unconditionally on its dock — a hidden chat would render with no rail icon).
  Restore is via the command palette (ADR 0057): `openView()` un-hides before routing, so ⌘K → a
  hidden view's name brings it back to a dock (its core default dock, else the left rail). Persist
  migration **v13** adds the empty bucket to older layouts.
- **Configure…** (plugin views only) — opens the owning plugin's settings dialog (ADR 0059). The
  App-side `onRailContextMenu` resolves the owning plugin's id + name from the `plugin:<id>:<view>`
  rail id and passes them in `ctx`; the action sets a store-driven `configurePlugin`, mounted once at
  the app root — the same per-plugin dialog the Plugins manager uses, now reached from the rail.

## References

- `rabbit-hole.io` context-menu module (`apps/rabbit-hole/app/context-menu/` — the registry +
  imperative-open pattern this adopts).
- ADR 0034 (plugin UI as first-class React — the SDK that re-exports `registerContextMenu`, and
  the trust gate it inherits), ADR 0035 (rail surfaces — the `rail-surface` menu is customer #1),
  ADR 0057 (command palette — the restore point for hidden surfaces), ADR 0059 (the per-plugin
  settings dialog that "Configure…" opens).
