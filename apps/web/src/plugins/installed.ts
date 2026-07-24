import type { RuntimeStatus } from "../lib/types";

type Plugin = NonNullable<RuntimeStatus["plugins"]>[number];

// One Installed-table row: the runtime plugin joined with the freshness + inventory
// queries it renders with (LocalTab does the join; keeping the shape explicit makes
// the filter/sort helpers pure and unit-testable, like catalog.ts for Discover).
export type InstalledRow = {
  p: Plugin;
  /** update available (pluginUpdatesQuery → behind) */
  behind: boolean;
  /** declared pip deps not importable (installedPluginsQuery → deps_missing) */
  depsMissing: string[];
  /** bundle provenance (installedPluginsQuery → bundle, ADR 0040): this plugin was
   *  installed by a bundle — label shown as a chip, searchable below. */
  bundle?: { id: string; name?: string };
};

/** Display label for a row's bundle chip — the bundle's name, falling back to its id
 *  (locks written before the name was persisted only carry the id). */
export function bundleLabel(r: InstalledRow): string | null {
  if (!r.bundle) return null;
  return r.bundle.name || r.bundle.id || null;
}

export type InstalledStatus = "All" | "Loaded" | "Disabled" | "Attention";
export type InstalledSortKey = "name" | "status" | "contributions";
export type InstalledSort = { key: InstalledSortKey; dir: "asc" | "desc" };

// A row the operator should look at: load error, unfinished required config,
// an available update, or missing pip deps.
export function needsAttention(r: InstalledRow): boolean {
  return Boolean(r.p.error || r.p.incomplete || r.behind || r.depsMissing.length);
}

export function contributionCount(p: Plugin): number {
  return (p.loaded ? p.tools.length + p.skills : 0) + (p.views?.length ?? 0);
}

// Free-text search + status chip. Tool NAMES are searchable on purpose — "which
// plugin ships tool X?" is a real question once dozens of plugins are installed —
// and so is the BUNDLE ("show me everything cowork-stack installed").
export function filterInstalled(rows: InstalledRow[], q: string, status: InstalledStatus): InstalledRow[] {
  const needle = q.trim().toLowerCase();
  return rows.filter((r) => {
    if (status === "Loaded" && !r.p.loaded) return false;
    if (status === "Disabled" && r.p.loaded) return false;
    if (status === "Attention" && !needsAttention(r)) return false;
    if (!needle) return true;
    return `${r.p.name} ${r.p.id} ${r.p.version ?? ""} ${r.p.tools.join(" ")} ${r.bundle?.name ?? ""} ${r.bundle?.id ?? ""}`
      .toLowerCase()
      .includes(needle);
  });
}

// Each key defines its NATURAL order (what a first click on the header gives you);
// dir === "desc" reverses it. Ties always fall back to name so the order is stable.
const byName = (a: InstalledRow, b: InstalledRow) => a.p.name.localeCompare(b.p.name);
const NATURAL: Record<InstalledSortKey, (a: InstalledRow, b: InstalledRow) => number> = {
  name: byName,
  // Loaded before disabled — the old section split, now a sort; attention-worthy
  // rows float to the top of each half.
  status: (a, b) =>
    Number(b.p.loaded) - Number(a.p.loaded) ||
    Number(needsAttention(b)) - Number(needsAttention(a)) ||
    byName(a, b),
  contributions: (a, b) => contributionCount(b.p) - contributionCount(a.p) || byName(a, b),
};

export function sortInstalled(rows: InstalledRow[], sort: InstalledSort): InstalledRow[] {
  const mul = sort.dir === "asc" ? 1 : -1;
  return [...rows].sort((a, b) => mul * NATURAL[sort.key](a, b));
}

export function statusCounts(rows: InstalledRow[]): Record<InstalledStatus, number> {
  return {
    All: rows.length,
    Loaded: rows.filter((r) => r.p.loaded).length,
    Disabled: rows.filter((r) => !r.p.loaded).length,
    Attention: rows.filter(needsAttention).length,
  };
}
