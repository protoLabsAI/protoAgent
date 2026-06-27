import { ArrowLeftRight, ChevronDown, ChevronUp, EyeOff, SlidersHorizontal } from "lucide-react";

import { useUI } from "../state/uiStore";
import { registerContextMenu } from "./registry";
import type { MenuEntry } from "./types";

// First customer (ADR 0036 D6 / ADR 0035 D2): right-click a rail icon → reorder it within its
// rail (Move up/down), move it to another dock, Configure the owning plugin, or Hide it from the
// rails (without disabling the plugin). Chat is movable across all three docks like any other
// surface; plugin views carry their owning plugin's id/name in `ctx` (resolved by the App-side
// trigger) so Configure can open that plugin's settings dialog.
registerContextMenu({
  type: "rail-surface",
  items: (ctx: {
    id: string;
    side: "left" | "right" | "bottom";
    pluginId?: string;
    pluginName?: string;
  }): MenuEntry[] => {
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
    // Management actions, gathered so the divider only shows when at least one applies:
    // Configure (plugin views only) opens the owning plugin's settings dialog; Hide moves the
    // surface to railOrder.hidden (restore from ⌘K or "Move to …"). Chat is never hidden — it
    // mounts unconditionally on its dock, so a hidden chat would render with no rail icon.
    const manage: MenuEntry[] = [];
    if (ctx.pluginId) {
      const pid = ctx.pluginId;
      const pname = ctx.pluginName ?? ctx.pluginId;
      manage.push({
        id: "configure",
        label: "Configure…",
        icon: <SlidersHorizontal size={14} />,
        run: () => useUI.getState().openPluginConfig(pid, pname),
      });
    }
    if (ctx.id !== "chat") {
      manage.push({
        id: "hide",
        label: "Hide",
        icon: <EyeOff size={14} />,
        run: () => useUI.getState().hideSurface(ctx.id),
      });
    }
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
      ...(manage.length ? [{ id: "manage-div", divider: true } as MenuEntry, ...manage] : []),
    ];
  },
});
