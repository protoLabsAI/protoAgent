import type { CatalogPlugin } from "../lib/types";

// Filter the official-plugin catalog by free-text query + category (ADR 0059).
// Pure — unit-tested and shared by the Discover section.
export function filterCatalog(plugins: CatalogPlugin[], q: string, category: string): CatalogPlugin[] {
  const needle = q.trim().toLowerCase();
  return plugins.filter((p) => {
    if (category !== "All" && (p.category || "Other") !== category) return false;
    if (!needle) return true;
    return `${p.name} ${p.tagline ?? ""} ${p.id} ${(p.adds ?? []).join(" ")}`.toLowerCase().includes(needle);
  });
}

// The category chips for a catalog: "All" + the distinct categories, sorted.
export function catalogCategories(plugins: CatalogPlugin[]): string[] {
  return ["All", ...Array.from(new Set(plugins.map((p) => p.category || "Other"))).sort()];
}

export type CatalogCardState =
  | { kind: "install" }
  | { kind: "state"; label: string; tone: "success" | "muted"; title: string; why?: string };

// What a Discover card's foot shows. A plugin that ships in core is never offered
// Install. It shows its real on/off state instead, and when another bundled plugin's
// `enables:` is the reason it's on, it says so (#3450: execute_code is on because
// cowork enables it). The Installed tab is where it's turned on or off.
export function catalogCardState(p: CatalogPlugin): CatalogCardState {
  if (p.bundled) {
    if (!p.enabled) {
      return {
        kind: "state",
        label: "bundled · off",
        tone: "muted",
        title: `${p.name} ships with protoAgent and is off. Turn it on from the Installed tab.`,
      };
    }
    const by = (p.enabled_by ?? []).filter(Boolean);
    const why = by.length ? `on because ${by.join(", ")} ${by.length === 1 ? "enables" : "enable"} it` : undefined;
    return {
      kind: "state",
      label: "bundled · on",
      tone: "success",
      title: why ? `${p.name} ships with protoAgent: ${why}.` : `${p.name} ships with protoAgent and is on.`,
      why,
    };
  }
  if (p.installed) {
    return {
      kind: "state",
      label: p.enabled ? "installed · on" : "installed · off",
      tone: p.enabled ? "success" : "muted",
      title: p.enabled ? `${p.name} is installed and on.` : `${p.name} is installed and off. Turn it on from the Installed tab.`,
    };
  }
  return { kind: "install" };
}
