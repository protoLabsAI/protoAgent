# `src/ext/` — fork extension seam

Add your fork's **own console components without editing core** (ADR 0038 D3). Drop a `*.tsx` file
here; the console auto-loads it at startup. **Core ships this directory with no `*.tsx` files**, so
`git pull upstream` never conflicts on your additions — and you rebuild your own app.

This is the **trusted, build-time, in-process** path — for *your* fork. (For shippable, sandboxed,
git-installable extensions, write a **plugin** instead — see `docs/guides/building-react-plugin-views.md`.)

## Example — add a rail surface

```tsx
// src/ext/my-dashboard.tsx
import { BarChart3 } from "lucide-react";
import { registerSurface, registerContextMenu } from "./index";

registerSurface({
  id: "my-dashboard",
  label: "Dashboard",
  icon: <BarChart3 size={18} />,
  placement: "left",                  // or "right"
  render: () => <div className="stage-body">…your React, in-process…</div>,
});

// You can also contribute context-menu items (ADR 0036):
registerContextMenu({
  type: "rail-surface",
  items: [{ id: "my-action", label: "My action", run: () => { /* … */ } }],
});
```

That's it — rebuild and your surface appears in the rail. No core files touched.

## Example — add a keybinding (ADR 0063)

`registerKeybinding` is a peer of the registries above: a fork/plugin binds its own default
shortcut through the same seam core uses. Every registered binding automatically appears in
**Settings ▸ Keyboard** (rebindable, with conflict detection) and fires through the global host.

```tsx
import { registerKeybinding } from "./index";

registerKeybinding({
  id: "my-dashboard.toggle",     // stable id — the key for user overrides + dedup
  label: "Toggle Dashboard",
  group: "My fork",              // its own section in Settings ▸ Keyboard
  defaultKeys: "mod+shift+d",    // normalized combo (mod = ⌘ on mac, ctrl elsewhere)
  scope: "my-dashboard",         // optional: fire only within a `data-kb-scope` panel
  run: () => { /* … open/focus the surface … */ },
});
```

A user can rebind it in Settings; if the combo collides with another binding in an overlapping
scope, the rebind UI blocks it and names the conflict.

## Example — add a ⌘K command (ADR 0061)

`registerPaletteCommand` adds a row to the root command palette's **Commands** group. Core's own
deep-links (Plugins: Discover, Settings…) register through this same call — there is no core-only
path.

```tsx
import { BarChart3 } from "lucide-react";
import { registerPaletteCommand, registerPaletteCommands, registerPaletteSource } from "./index";

registerPaletteCommand({
  id: "my-dashboard.open",       // stable id — dedup key, LAST registration of an id wins
  label: "Open Dashboard",
  icon: <BarChart3 size={16} />,
  hint: "go to",                 // muted trailing text; a `disabled: true` row says WHY here
  keybinding: "my-dashboard.toggle", // a registerKeybinding id — the row shows its LIVE combo
  flag: "my-fork-beta",          // optional: listed only while this developer flag is ON
  hostOnly: false,               // optional: drop the row in a workspace/sister-agent window
  run: (ctx) => { /* … navigate … */ ctx.close(); },
});
```

Gates are **read** at render (`visiblePaletteCommands`), never at registration — developer flags
fail closed while `/api/flags` is in flight, so a row filtered at registration would stay hidden
forever.

### A live list of rows

For a list that changes while the console runs, register the whole thing as a **block** and
re-register it when your data moves:

```tsx
const rows = () => openTabs().map((t) => ({
  id: `my-fork:tab:${t.id}`,
  label: t.title,
  group: "Tabs",
  run: (ctx) => { focusTab(t.id); ctx.close(); },
}));

let off = registerPaletteCommands(rows());
myStore.subscribe(() => {
  if (unchanged()) return;        // your store probably notifies far more often than the list moves
  const stale = off;              // new block first, old handle second: no row ever blinks out
  off = registerPaletteCommands(rows());
  stale();
});
```

`registerPaletteCommands` is one version bump for the whole block, re-inserts it as a unit in the
order you hand in (so it renders under one group header, and a reordered list really reorders), and
its unregister removes only the rows it still owns — so the refresh above deletes exactly what went
away. Core's own list is exactly this shape: `app/chatTabPalette.ts` is a row per open chat tab,
subscribed to the chat store. Read it when yours needs one to copy.

### …and when you can't be notified

Only when nothing can tell you your data moved, register a **source** — rows computed at read time:

```tsx
registerPaletteSource(() => openTabs().map((t) => ({
  id: `my-fork:tab:${t.id}`,
  label: `Go to ${t.title}`,
  run: (ctx) => { focusTab(t.id); ctx.close(); },
})));
```

A source is called **every time the palette is read** — once when ⌘K opens and again on each
keystroke — so its rows follow your data with no notification of any kind on your side. (It is not
called *when your data changes*: nothing watches it. A row that changes while the palette is open
and untouched appears on the next keystroke.) That is why a source has to be **cheap and
synchronous**: no fetches, no store writes, no `async`. Keep it to mapping state you already have.

**Prefer the block whenever you have the choice.** A source is served through a
`CommandProvider`, which the palette debounces by 120ms — and for that window the *previous*
query's rows are still on screen, because provider rows are ordered but never re-filtered (that is
on purpose: a source is usually a remote or fuzzy search, and re-filtering would delete the hits it
exists to contribute). So for a beat after every keystroke the palette lists rows the query
excludes, and if nothing else matches, Enter runs one. Statics have none of that: they are
client-filtered per keystroke with nothing retained, and ranked in the same pass as every other row
rather than appended after it. Core moved its chat rows off the source path for exactly this
reason.

A source's row is also **built at read time and run a keystroke later**, so whatever it points at
can be gone by the time the operator hits Enter. Re-check it where you act on it, not where you
build the row — core's chat rows re-validate the session id before switching tabs, because the
store they hand it to does not.

A broken source is contained: it is skipped, the sources registered after it still run, and the
palette keeps every other row. That covers a `throw` **and** a return that isn't an array — an
`async` source (which returns a Promise), an id-keyed object, `false` for "nothing to show" — since
the seam is a build-time edge where a fork's own mistake typechecks.

All three return an unregister fn, so a feature can withdraw its commands.
