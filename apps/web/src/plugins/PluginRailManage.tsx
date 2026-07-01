import { ConfirmDialog } from "@protolabsai/ui/overlays";
import { useEffect } from "react";

import { useUI } from "../state/uiStore";
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
      {pluginUninstall ? `Uninstall ${pluginUninstall.name}? This cannot be undone.` : undefined}
    </ConfirmDialog>
  );
}
