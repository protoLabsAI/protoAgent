import { useMutation, useQueryClient } from "@tanstack/react-query";
import { useToast } from "@protolabsai/ui/overlays";

import { api } from "../lib/api";
import { errMsg } from "../lib/format";
import { queryKeys, runtimeStatusQuery } from "../lib/queries";

// A plugin the actions target — just its id (for the API) + name (for the toast).
export type PluginRef = { id: string; name: string };

// Shared update + uninstall mutations for a single plugin (#1521 / #1522, ADR 0027).
// Used by BOTH the Plugins manager rows (PluginsSurface) and the rail context-menu
// actions (PluginRailManage), so the toast copy + query-refresh are identical wherever
// a plugin is updated/removed. On success we refresh: runtime (the rail icons + the
// loaded set), the installed inventory (removable list), the freshness poll, and the
// settings schema (a new/removed plugin changes which config fields exist, #1423).
export function usePluginManage() {
  const qc = useQueryClient();
  const toast = useToast();

  const refreshAll = () => {
    qc.invalidateQueries({ queryKey: runtimeStatusQuery().queryKey });
    qc.invalidateQueries({ queryKey: queryKeys.installedPlugins });
    qc.invalidateQueries({ queryKey: queryKeys.pluginUpdates });
    qc.invalidateQueries({ queryKey: queryKeys.settings });
  };

  // Pull the latest code at the plugin's recorded ref + hot-reload (same path as enable).
  const update = useMutation({
    mutationFn: (p: PluginRef) => api.updatePlugin(p.id),
    onSuccess: (res, p) => {
      refreshAll();
      toast(
        res.restart_recommended
          ? { tone: "info", title: "Plugin updated", message: `${p.name}${res.version ? ` to v${res.version}` : ""} — restart to fully load its console view or background surface.` }
          : { tone: "success", title: "Plugin updated", message: `${p.name}${res.version ? ` to v${res.version}` : ""}${res.reloaded ? " (hot-reloaded)" : ""}.` },
      );
    },
    onError: (err: unknown, p) => toast({ tone: "error", title: "Couldn't update plugin", message: `${p.name}: ${errMsg(err)}` }),
  });

  // Uninstall (DELETE) — removes the code + plugins.lock / enabled refs. Refused
  // server-side for in-tree built-ins, so callers only offer it for writable-dir plugins.
  const remove = useMutation({
    mutationFn: (p: PluginRef) => api.uninstallPlugin(p.id),
    onSuccess: (_res, p) => {
      refreshAll();
      toast({ tone: "success", title: "Plugin uninstalled", message: `${p.name} removed.` });
    },
    onError: (err: unknown, p) => toast({ tone: "error", title: "Couldn't uninstall plugin", message: `${p.name}: ${errMsg(err)}` }),
  });

  return { update, remove };
}
