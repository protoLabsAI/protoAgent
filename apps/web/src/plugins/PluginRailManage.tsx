import { ConfirmDialog } from "@protolabsai/ui/overlays";
import { useQuery } from "@tanstack/react-query";
import { useEffect } from "react";

import { installedPluginsQuery } from "../lib/queries";
import { useUI } from "../state/uiStore";
import { uninstallConfirmText } from "./installed";
import { usePluginManage } from "./usePluginManage";

// Root-mounted host for the rail context-menu plugin actions (#1521 / #1522, ADR 0036).
// A right-click "Update available" / "Uninstall…" on a plugin's rail icon records the
// pending action in the UI store; this component fires the update mutation (no confirm —
// an update is non-destructive and reversible by a re-install) or renders the uninstall
// confirm. Mounted once in App so the actions work regardless of whether the Plugins
// settings panel is open. Success/failure surface via the shared toast, and the rail +
// installed list refresh via the mutation's query invalidation.
export function PluginRailManage() {
  const pluginUpdate = useUI((s) => s.pluginUpdate);
  const clearPluginUpdate = useUI((s) => s.clearPluginUpdate);
  const pluginUninstall = useUI((s) => s.pluginUninstall);
  const clearPluginUninstall = useUI((s) => s.clearPluginUninstall);
  const { update, remove } = usePluginManage();
  // The inventory row for the pending uninstall — only fetched while a confirm is open —
  // so a plugin that now ships with protoAgent gets the truthful "removes the old copy;
  // the built-in keeps running" text instead of "cannot be undone".
  const inventory = useQuery({ ...installedPluginsQuery(), enabled: pluginUninstall !== undefined });
  const pendingRow = pluginUninstall
    ? inventory.data?.plugins.find((e) => e.id === pluginUninstall.id)
    : undefined;

  // Fire the requested update, consuming the trigger first so it runs exactly once
  // (the next render sees `pluginUpdate` cleared and early-returns). The toast reports
  // the outcome; no modal — an update doesn't need a confirm.
  useEffect(() => {
    if (!pluginUpdate) return;
    const target = pluginUpdate;
    clearPluginUpdate();
    update.mutate(target);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [pluginUpdate]);

  return (
    <ConfirmDialog
      open={pluginUninstall !== undefined}
      title="Uninstall plugin?"
      confirmLabel="Uninstall"
      destructive
      onConfirm={() => {
        if (pluginUninstall) remove.mutate(pluginUninstall);
        clearPluginUninstall();
      }}
      onClose={clearPluginUninstall}
    >
      {/* The same text the Plugins table shows — including the "this removes the old
          copy, the built-in keeps running" wording for a plugin that moved into core.
          Unconditional on purpose: picking the generic destructive line while the
          inventory is still loading would warn about deleting code that isn't going. */}
      {pluginUninstall ? uninstallConfirmText(pluginUninstall.name, pendingRow) : undefined}
    </ConfirmDialog>
  );
}
