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

/** What this data has to show. Every tab is conditional on having rows behind it —
 *  an empty tab is worse than no tab, and that reasoning is not special to Fleet:
 *  a box that has never called a tool has nothing to put under "By tool" either.
 *  "Recent turns" is always present as the anchor; it carries the store's own
 *  empty/disabled message when there is nothing in it. */
export function telemetryTabItems(views: {
  hasModels: boolean;
  hasTools: boolean;
  hasFleet: boolean;
}): TelemetryTabItem[] {
  const items: TelemetryTabItem[] = [{ id: "turns", label: "Recent turns" }];
  if (views.hasModels) items.push({ id: "models", label: "By model" });
  if (views.hasTools) items.push({ id: "tools", label: "By tool" });
  if (views.hasFleet) items.push({ id: "fleet", label: "Fleet" });
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
export function hasTelemetryViews(hasTurns: boolean, hasFleet: boolean): boolean {
  return hasTurns || hasFleet;
}
