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
