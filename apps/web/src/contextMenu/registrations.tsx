import { ArrowLeftRight, ChevronDown, ChevronUp } from "lucide-react";

import { useUI } from "../state/uiStore";
import { registerContextMenu } from "./registry";
import type { MenuEntry } from "./types";

// First customer (ADR 0036 D6 / ADR 0035 D2): right-click a rail icon → reorder it within its
// rail (Move up/down) and move it to another dock (append to the end). Chat is movable across all
// three docks like any other surface; plugin views follow their manifest placement (no items yet).
registerContextMenu({
  type: "rail-surface",
  items: (ctx: { id: string; side: "left" | "right" | "bottom" }): MenuEntry[] => {
    if (!ctx) return [];
    const ui = useUI.getState();
    const list = ui.railOrder[ctx.side] ?? [];
    const i = list.indexOf(ctx.id);
    if (i < 0) return []; // not yet tracked (a freshly-appeared plugin, pre-reconcile)
    // Open the moved surface on its new dock (un-collapsing it). The App clamps each dock's
    // active to a member, so the source dock self-heals when the surface leaves.
    const moveTo = (side: "left" | "right" | "bottom") => {
      const u = useUI.getState();
      u.moveSurface(ctx.id, side);
      if (side === "left") { u.setSurface(ctx.id); u.setLeftCollapsed(false); }
      else if (side === "right") { u.setRightPanel(ctx.id); u.setRightCollapsed(false); }
      else { u.setBottomPanel(ctx.id); u.setBottomCollapsed(false); }
    };
    const DOCKS: { side: "left" | "right" | "bottom"; label: string }[] = [
      { side: "left", label: "Move to left rail" },
      { side: "right", label: "Move to right rail" },
      { side: "bottom", label: "Move to bottom dock" },
    ];
    // Any surface — core, plugin, or chat — reorders within its dock and moves across, including
    // chat to the bottom dock (its slot mounts unconditionally there too). Moving chat across docks
    // remounts it: a brief blip on an in-flight stream; a deliberate action.
    return [
      {
        id: "move-up",
        label: "Move up",
        icon: <ChevronUp size={14} />,
        disabled: i <= 0,
        run: () => useUI.getState().reorderSurface(ctx.id, -1),
      },
      {
        id: "move-down",
        label: "Move down",
        icon: <ChevronDown size={14} />,
        disabled: i >= list.length - 1,
        run: () => useUI.getState().reorderSurface(ctx.id, 1),
      },
      { id: "rail-div", divider: true },
      ...DOCKS.filter((d) => d.side !== ctx.side)
        .map((d): MenuEntry => ({
          id: `move-${d.side}`,
          label: d.label,
          icon: <ArrowLeftRight size={14} />,
          run: () => moveTo(d.side),
        })),
    ];
  },
});
