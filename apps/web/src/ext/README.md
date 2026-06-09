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
