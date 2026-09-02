import type { FleetTelemetry, TelemetrySummary } from "../lib/types";

// Which views the Telemetry surface offers, and which one it opens on (#3329).
//
// The surface used to be one long scroll: insights, eleven metric cards, then
// THREE stacked tables (by model, by tool, and the ten-column recent-turns table)
// plus the fleet rollup. The headline numbers — the reason you open Telemetry —
// were the only part you could see without scrolling, and everything below them
// competed for the same page.
//
// The headline stays pinned; these are the drill-downs behind a tab strip. Pure
// data → items, kept out of the component because the console has no
// component-rendering suite (same reasoning as traceUrl.ts).

export type TelemetryTabId = "turns" | "models" | "tools" | "fleet";

export type TelemetryTabItem = { id: TelemetryTabId; label: string };

/** The tab strip for this data. Fleet appears only on a fleet install — a single
 *  box has no members to roll up, and an empty tab is worse than no tab. */
export function telemetryTabItems(hasFleet: boolean): TelemetryTabItem[] {
  const items: TelemetryTabItem[] = [
    { id: "turns", label: "Recent turns" },
    { id: "models", label: "By model" },
    { id: "tools", label: "By tool" },
  ];
  if (hasFleet) items.push({ id: "fleet", label: "Fleet" });
  return items;
}

/** The tab to open on.
 *
 * Normally "turns" — the per-turn table is what the surface is for. But a hub whose
 * OWN store is empty while its members have turns opens on "fleet": the fleet
 * rollup deliberately renders outside this box's empty-state gates (a peer can have
 * turns when this box has none), and burying the only populated view behind a click
 * would undo that. */
export function defaultTelemetryTab(hasTurns: boolean, hasFleet: boolean): TelemetryTabId {
  return !hasTurns && hasFleet ? "fleet" : "turns";
}

/** Whether the surface has anything at all to show behind a tab strip. With no
 *  turns and no fleet there is one message to render, and a strip of empty tabs
 *  above it is furniture. */
export function hasTelemetryViews(summary: TelemetrySummary | null | undefined, fleet: FleetTelemetry): boolean {
  return Boolean((summary?.turns ?? 0) > 0 || fleet.fleet);
}
