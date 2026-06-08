import { ArrowLeftRight, ChevronDown, ChevronUp } from "lucide-react";

import { useUI } from "../state/uiStore";
import { registerContextMenu } from "./registry";
import type { MenuEntry } from "./types";

// First customer (ADR 0036 D6 / ADR 0035 D2): right-click a rail icon → reorder it within its
// rail (Move up/down) and move it to the other rail (append to the bottom). Chat is pinned to the
// left rail (no move-across); plugin views follow their manifest placement (no items yet).
registerContextMenu({
  type: "rail-surface",
  items: (ctx: { id: string; side: "left" | "right" }): MenuEntry[] => {
    if (!ctx || String(ctx.id).startsWith("plugin:")) return [];
    const ui = useUI.getState();
    const list = ui.railOrder[ctx.side] ?? [];
    const i = list.indexOf(ctx.id);
    if (i < 0) return [];
    const entries: MenuEntry[] = [
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
    ];
    if (ctx.id !== "chat") {
      const to = ctx.side === "left" ? "right" : "left";
      entries.push({ id: "rail-div", divider: true });
      entries.push({
        id: "move-rail",
        label: `Move to ${to} rail`,
        icon: <ArrowLeftRight size={14} />,
        run: () => {
          const u = useUI.getState();
          u.moveSurface(ctx.id, to);
          if (to === "left") {
            u.setSurface(ctx.id);
            if (u.rightPanel === ctx.id) u.setRightPanel("notes");
          } else {
            u.setRightPanel(ctx.id);
            u.setRightCollapsed(false);
            if (u.surface === ctx.id) u.setSurface("chat");
          }
        },
      });
    }
    return entries;
  },
});
