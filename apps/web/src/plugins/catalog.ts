import type { CatalogPlugin } from "../lib/types";

// Filter the official-plugin catalog by free-text query + category (ADR 0059).
// Pure — unit-tested and shared by the Discover section.
export function filterCatalog(plugins: CatalogPlugin[], q: string, category: string): CatalogPlugin[] {
  const needle = q.trim().toLowerCase();
  return plugins.filter((p) => {
    if (category !== "All" && (p.category || "Other") !== category) return false;
    if (!needle) return true;
    return `${p.name} ${p.tagline ?? ""} ${p.id}`.toLowerCase().includes(needle);
  });
}

// The category chips for a catalog: "All" + the distinct categories, sorted.
export function catalogCategories(plugins: CatalogPlugin[]): string[] {
  return ["All", ...Array.from(new Set(plugins.map((p) => p.category || "Other"))).sort()];
}
