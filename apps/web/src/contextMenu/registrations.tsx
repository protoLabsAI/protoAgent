import { ArrowLeftRight, ChevronDown, ChevronsLeft, ChevronsRight, ChevronUp, Download, Eye, EyeOff, Pencil, Plus, Puzzle, RefreshCw, SlidersHorizontal, Trash2, X } from "lucide-react";

import { openView } from "../app/usePaletteRegistry";
import { useUI } from "../state/uiStore";
import { registerContextMenu } from "./registry";
import type { MenuEntry } from "./types";

// First customer (ADR 0036 D6 / ADR 0035 D2): right-click a rail icon → reorder it within its
// rail (Move up/down), move it to another dock, Configure the owning plugin, or Hide it from the
// rails (without disabling the plugin). Chat is movable across all three docks like any other
// surface; plugin views carry their owning plugin's id/name in `ctx` (resolved by the App-side
// trigger) so Configure can open that plugin's settings dialog.
//
// Plugin lifecycle (#1521 / #1522): the App-side trigger also resolves the plugin's installed
// version, whether the freshness poll says it's behind (`pluginUpdatable`), and whether it lives
// in the writable plugins dir (`pluginRemovable`). So the menu shows the version, an "Update
// available" action when behind, and a destructive "Uninstall…" — both gated so an in-tree
// built-in (which the server refuses to update/uninstall) never offers them.
registerContextMenu({
  type: "rail-surface",
  items: (ctx: {
    id: string;
    side: "left" | "right" | "bottom";
    pluginId?: string;
    pluginName?: string;
    pluginVersion?: string;
    pluginBuiltin?: boolean;
    pluginRemovable?: boolean;
    pluginUpdatable?: boolean;
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
    // Management actions. "Manage plugins…" (open the all-plugins manager, Settings ▸ Plugins)
    // is always present; Configure (plugin views only) opens the owning plugin's settings dialog;
    // Hide moves the surface to railOrder.hidden (restore from ⌘K or "Move to …"). Chat is never
    // hidden — it mounts unconditionally on its dock, so a hidden chat would render with no rail icon.
    // Built-in / uninstall / update, all gated on the plugin's origin, cluster under Configure.
    const manage: MenuEntry[] = [];
    if (ctx.pluginId) {
      const pid = ctx.pluginId;
      const pname = ctx.pluginName ?? ctx.pluginId;
      // Installed version — informational (a disabled, non-clickable header), so the menu
      // answers "which version am I on?" without opening the manager.
      if (ctx.pluginVersion) {
        manage.push({ id: "plugin-version", label: `Version v${ctx.pluginVersion}`, disabled: true, run: () => {} });
      }
      manage.push({
        id: "configure",
        label: "Configure…",
        icon: <SlidersHorizontal size={14} />,
        run: () => useUI.getState().openPluginConfig(pid, pname),
      });
      // Update — only when the freshness poll says this plugin is behind its ref AND it's
      // not an in-tree built-in (the server refuses to update those). Fires via the store;
      // a root PluginRailManage runs the mutation + toast (up-to-date/pinned → no item).
      if (ctx.pluginUpdatable && !ctx.pluginBuiltin) {
        manage.push({
          id: "update",
          label: "Update available",
          icon: <RefreshCw size={14} />,
          run: () => useUI.getState().requestPluginUpdate(pid, pname),
        });
      }
    }
    // A rail-wide escape hatch on every icon: the all-plugins counterpart to the per-plugin
    // "Configure…" above — opens Settings ▸ Plugins.
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
    // Uninstall — a destructive action set off by its own divider, offered only for a
    // writable-dir plugin (git-installed / local copy) that isn't an in-tree built-in
    // (the server refuses those, so they only get Disable in the manager). The store
    // trigger opens a "This cannot be undone." confirm rendered by PluginRailManage.
    if (ctx.pluginId && ctx.pluginRemovable && !ctx.pluginBuiltin) {
      const pid = ctx.pluginId;
      const pname = ctx.pluginName ?? ctx.pluginId;
      manage.push({ id: "uninstall-div", divider: true });
      manage.push({
        id: "uninstall",
        label: "Uninstall…",
        icon: <Trash2 size={14} />,
        danger: true,
        run: () => useUI.getState().requestPluginUninstall(pid, pname),
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
// Plugins. The App-side trigger resolves each hidden id's label (core/plugin/ext metadata lives
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
    // Plugins — to install, enable/disable, configure, or update plugins.
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

// Right-click a chat session tab → New chat / New incognito chat / Rename / Incognito toggle /
// Close + bulk Close others/left/right (ADR 0036). ChatSurface owns the behavior and passes it
// in `ctx` as closures: Close reuses its confirm-dialog flow, Rename triggers the DS TabBar's
// inline editor (a synthetic dblclick on the tab), the incognito entries flip the per-thread
// flag (ADR 0069 D3b — every send while ON carries metadata.incognito). The bulk closers arrive
// pre-resolved: ChatSurface passes each `onCloseOthers/Left/Right` closure only when that action
// has tabs to close (e.g. no `onCloseLeft` on the leftmost tab), so the menu hides an entry
// simply by not receiving its closure. Right-clicking empty tab-bar space carries only the
// `onNew*` closures. (The DS TabBar exposes no per-tab context-menu hook — a DS gap noted for
// contribute-back; ChatSurface delegates from the tab-bar wrapper meanwhile.)
registerContextMenu({
  type: "chat-tab",
  items: (ctx: {
    sessionId?: string;
    incognito?: boolean;
    onNew?: () => void;
    onNewIncognito?: () => void;
    onToggleIncognito?: () => void;
    onRename?: () => void;
    onExport?: () => void;
    onClose?: () => void;
    onCloseOthers?: () => void;
    onCloseLeft?: () => void;
    onCloseRight?: () => void;
  }): MenuEntry[] => {
    const out: MenuEntry[] = [
      { id: "new", label: "New chat", icon: <Plus size={14} />, run: () => ctx?.onNew?.() },
      {
        id: "new-incognito",
        label: "New incognito chat",
        icon: <EyeOff size={14} />,
        run: () => ctx?.onNewIncognito?.(),
      },
    ];
    if (ctx?.sessionId) {
      out.push({ id: "rename", label: "Rename", icon: <Pencil size={14} />, run: () => ctx.onRename?.() });
      out.push({
        id: "incognito",
        label: ctx.incognito ? "Turn incognito off" : "Turn incognito on",
        icon: ctx.incognito ? <Eye size={14} /> : <EyeOff size={14} />,
        run: () => ctx.onToggleIncognito?.(),
      });
      if (ctx.onExport) {
        out.push({ id: "export", label: "Export as Markdown", icon: <Download size={14} />, run: () => ctx.onExport?.() });
      }
      out.push({ id: "tab-div", divider: true });
      out.push({ id: "close", label: "Close chat", icon: <X size={14} />, danger: true, run: () => ctx.onClose?.() });
      // Bulk closers, each present only when it has targets (ChatSurface passes the closure
      // only then). A divider precedes them, but only if at least one is offered — so the menu
      // never shows a trailing separator with nothing under it.
      const bulk: MenuEntry[] = [];
      if (ctx.onCloseOthers) {
        bulk.push({ id: "close-others", label: "Close others", icon: <X size={14} />, danger: true, run: () => ctx.onCloseOthers?.() });
      }
      if (ctx.onCloseLeft) {
        bulk.push({ id: "close-left", label: "Close left", icon: <ChevronsLeft size={14} />, danger: true, run: () => ctx.onCloseLeft?.() });
      }
      if (ctx.onCloseRight) {
        bulk.push({ id: "close-right", label: "Close right", icon: <ChevronsRight size={14} />, danger: true, run: () => ctx.onCloseRight?.() });
      }
      if (bulk.length) {
        out.push({ id: "bulk-div", divider: true });
        out.push(...bulk);
      }
    }
    return out;
  },
});
