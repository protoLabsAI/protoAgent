import { ArrowLeftRight } from "lucide-react";

import { useUI } from "../state/uiStore";
import { registerContextMenu } from "./registry";

// First customer (ADR 0036 D6 / ADR 0035 D2): right-click a rail icon → move it to the other rail.
// Chat is pinned left and plugin views follow their placement, so they offer no move item.
registerContextMenu({
  type: "rail-surface",
  items: (ctx: { id: string; side: "left" | "right" }) => {
    if (!ctx || ctx.id === "chat" || String(ctx.id).startsWith("plugin:")) return [];
    const to = ctx.side === "left" ? "right" : "left";
    return [
      {
        id: "move-rail",
        label: `Move to ${to} rail`,
        icon: <ArrowLeftRight size={14} />,
        run: () => {
          const ui = useUI.getState();
          ui.moveSurface(ctx.id, to);
          if (to === "left") {
            ui.setSurface(ctx.id);
            if (ui.rightPanel === ctx.id) ui.setRightPanel("notes");
          } else {
            ui.setRightPanel(ctx.id);
            ui.setRightCollapsed(false);
            if (ui.surface === ctx.id) ui.setSurface("chat");
          }
        },
      },
    ];
  },
});
