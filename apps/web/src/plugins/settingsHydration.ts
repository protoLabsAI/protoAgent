import type { SettingsGroup } from "../lib/types";

// Open-time hydration guard for the per-plugin Configure dialog (#1643). Pure —
// unit-tested; the dialog wires it to the React Query cache.
//
// The settings schema query is cached with a 5-minute staleTime (the GET does a
// gateway round-trip server-side), and the dialog opens from several entry points
// (the plugin manager row, the rail context menu, the util-bar widget) — so a
// cached schema can predate the plugin's install and carry no group for it, which
// rendered the dialog EMPTY until a full page refresh. Every install path also
// invalidates the schema (usePluginRefresh), but the dialog is the last line of
// defense for any path that doesn't.
//
// Refetch exactly when a cached schema exists AND lacks this plugin's group:
//  - no cache yet     → the mounting suspense query fetches fresh anyway;
//  - group present    → the cache is good — don't burn the gateway round-trip;
//  - group missing    → the cache likely predates the install (or the plugin truly
//    has no settings — one refetch makes "Nothing to configure here" authoritative).
export function pluginSchemaNeedsRefetch(
  cached: { groups: SettingsGroup[] } | undefined,
  pluginId: string,
): boolean {
  return cached !== undefined && !cached.groups.some((g) => g.plugin_id === pluginId);
}
