import { ArrowLeftRight, ChevronDown, ChevronUp, Eye, EyeOff, Pencil, Plus, Puzzle, SlidersHorizontal, X } from "lucide-react";

import { openView } from "../app/usePaletteRegistry";
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
    // Management actions. "Manage plugins…" (open the all-plugins manager, Settings ▸ Integrations)
    // is always present; Configure (plugin views only) opens the owning plugin's settings dialog;
    // Hide moves the surface to railOrder.hidden (restore from ⌘K or "Move to …"). Chat is never
    // hidden — it mounts unconditionally on its dock, so a hidden chat would render with no rail icon.
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
    // A rail-wide escape hatch on every icon: the all-plugins counterpart to the per-plugin
    // "Configure…" above — opens Settings ▸ Integrations.
    manage.push({
      id: "manage-plugins",
      label: "Manage plugins…",
      icon: <Puzzle size={14} />,
      run: () => useUI.getState().openGlobalSettings("plugins"),
    });
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

// Right-click the EMPTY rail background (not an icon) → the rail menu: one "Show hidden view" entry
// per hidden surface (railOrder.hidden), each restored onto the dock whose background was clicked
// (`ctx.side`) and then opened, plus a rail-wide "Manage plugins…" action that opens Settings ▸
// Integrations. The App-side trigger resolves each hidden id's label (core/plugin/ext metadata lives
// there) + the clicked side into `ctx`. When nothing is hidden, a disabled hint shows so that part
// still confirms the feature. The discoverable counterpart to ⌘K (ADR 0035/0036).
registerContextMenu({
  type: "rail-background",
  items: (ctx: { side?: "left" | "right" | "bottom"; hidden?: { id: string; label: string }[] }): MenuEntry[] => {
    const hidden = ctx?.hidden ?? [];
    const out: MenuEntry[] = hidden.length
      ? [
          { id: "hidden-header", label: "Show hidden view", disabled: true, run: () => {} },
          { id: "hidden-div", divider: true },
          ...hidden.map(
            (h): MenuEntry => ({
              id: `show-${h.id}`,
              label: h.label,
              icon: <Eye size={14} />,
              // Restore to the dock the menu was opened on, then open it there.
              run: () => {
                useUI.getState().showSurface(h.id, ctx?.side);
                openView(h.id);
              },
            }),
          ),
        ]
      : [{ id: "none", label: "No hidden views", disabled: true, run: () => {} }];
    // A rail-wide action (not tied to one surface): open the plugin manager — Settings ▸
    // Integrations — to install, enable/disable, configure, or update plugins.
    out.push({ id: "manage-div", divider: true });
    out.push({
      id: "manage-plugins",
      label: "Manage plugins…",
      icon: <Puzzle size={14} />,
      run: () => useUI.getState().openGlobalSettings("plugins"),
    });
    return out;
  },
});

// Right-click a plugin's util-bar widget (a bottom-left pill) → Configure its plugin (ADR
// 0036/0059), mirroring the rail-icon Configure. The App-side trigger resolves the owning
// plugin's id/name from the `plugin:<id>:<view>` widget key into `ctx`.
registerContextMenu({
  type: "util-widget",
  items: (ctx: { pluginId?: string; pluginName?: string }): MenuEntry[] => {
    if (!ctx?.pluginId) return [];
    const pid = ctx.pluginId;
    const pname = ctx.pluginName ?? ctx.pluginId;
    return [
      {
        id: "configure",
        label: "Configure…",
        icon: <SlidersHorizontal size={14} />,
        run: () => useUI.getState().openPluginConfig(pid, pname),
      },
    ];
  },
});

// Right-click a chat session tab → New chat / Rename / Close (ADR 0036). ChatSurface owns the
// behavior and passes it in `ctx` as closures: Close reuses its confirm-dialog flow, Rename
// triggers the DS TabBar's inline editor (a synthetic dblclick on the tab). Right-clicking empty
// tab-bar space carries only `onNew`. (The DS TabBar exposes no per-tab context-menu hook — a
// DS gap noted for contribute-back; ChatSurface delegates from the tab-bar wrapper meanwhile.)
registerContextMenu({
  type: "chat-tab",
  items: (ctx: {
    sessionId?: string;
    onNew?: () => void;
    onRename?: () => void;
    onClose?: () => void;
  }): MenuEntry[] => {
    const out: MenuEntry[] = [
      { id: "new", label: "New chat", icon: <Plus size={14} />, run: () => ctx?.onNew?.() },
    ];
    if (ctx?.sessionId) {
      out.push({ id: "rename", label: "Rename", icon: <Pencil size={14} />, run: () => ctx.onRename?.() });
      out.push({ id: "tab-div", divider: true });
      out.push({ id: "close", label: "Close chat", icon: <X size={14} />, danger: true, run: () => ctx.onClose?.() });
    }
    return out;
  },
});
