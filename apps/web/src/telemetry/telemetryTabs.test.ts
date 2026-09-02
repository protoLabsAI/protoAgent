import { describe, expect, it } from "vitest";

import { defaultTelemetryTab, hasTelemetryViews, telemetryTabItems } from "./telemetryTabs";

import type { FleetTelemetry, TelemetrySummary } from "../lib/types";

const summary = (turns: number) => ({ turns } as TelemetrySummary);
const fleet = (on: boolean) => ({ fleet: on, members: [] } as unknown as FleetTelemetry);

describe("telemetryTabItems", () => {
  it("offers the three per-box views", () => {
    expect(telemetryTabItems(false).map((t) => t.id)).toEqual(["turns", "models", "tools"]);
  });

  it("adds Fleet only on a fleet install", () => {
    // A single box has no members to roll up, and an empty tab is worse than no tab.
    expect(telemetryTabItems(true).map((t) => t.id)).toContain("fleet");
    expect(telemetryTabItems(false).map((t) => t.id)).not.toContain("fleet");
  });
});

describe("defaultTelemetryTab", () => {
  it("opens on the turns table when this box has turns", () => {
    expect(defaultTelemetryTab(true, false)).toBe("turns");
    expect(defaultTelemetryTab(true, true)).toBe("turns");
  });

  it("opens on Fleet when this box is empty but its members are not", () => {
    // The fleet rollup deliberately renders outside this box's empty-state gates —
    // a peer can have turns when the hub has none. Opening on an empty turns table
    // would bury the only populated view behind a click.
    expect(defaultTelemetryTab(false, true)).toBe("fleet");
  });

  it("still opens on turns when there is nothing anywhere", () => {
    expect(defaultTelemetryTab(false, false)).toBe("turns");
  });
});

describe("hasTelemetryViews", () => {
  it("is false only when there is neither a turn nor a fleet", () => {
    // With nothing to show there is one message to render; a strip of empty tabs
    // above it is furniture.
    expect(hasTelemetryViews(summary(0), fleet(false))).toBe(false);
    expect(hasTelemetryViews(null, fleet(false))).toBe(false);
    expect(hasTelemetryViews(undefined, fleet(false))).toBe(false);
  });

  it("is true when either side has something", () => {
    expect(hasTelemetryViews(summary(3), fleet(false))).toBe(true);
    expect(hasTelemetryViews(summary(0), fleet(true))).toBe(true);
  });
});
