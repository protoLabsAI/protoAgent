import { useEffect } from "react";

import { onTopic } from "../lib/events";
import { usePluginRefresh } from "../plugins/usePluginManage";

// ADR 0096 D8 — when the AGENT changes plugin state (the devkit's scaffold_plugin /
// enable_plugin / reload_plugins / develop_plugin) or the autoupdate loop pulls a new
// version, the server publishes `plugin.changed` / `plugin.updated` on the bus. Without
// this watcher the console only learns on the next runtime-status fetch — which never
// comes: the runtime poll stops permanently once the graph is loaded, so a rail view the
// agent just enabled stayed invisible until a manual refresh. Subscribes to `plugin.#`
// and refetches the shared plugin invalidation set (runtime + installed + freshness +
// settings schema — the same four keys every console-initiated mutation refreshes), so
// the agent path and the console path converge on one refresh definition. Mounted once,
// app-wide, alongside the other bus watchers.
export function PluginChangeWatch() {
  const refreshAll = usePluginRefresh();
  useEffect(() => onTopic("plugin.#", () => refreshAll()), []); // eslint-disable-line react-hooks/exhaustive-deps
  return null;
}
