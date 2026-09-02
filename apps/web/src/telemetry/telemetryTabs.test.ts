import { describe, expect, it } from "vitest";

import { defaultTelemetryTab, hasTelemetryViews, telemetryTabItems } from "./telemetryTabs";

const views = (o: Partial<{ hasModels: boolean; hasTools: boolean; hasFleet: boolean }> = {}) => ({
  hasModels: false,
  hasTools: false,
  hasFleet: false,
  ...o,
});

describe("telemetryTabItems", () => {
  it("always anchors on the turns table", () => {
    // It carries the store's empty/disabled message, so it is the one tab that has
    // something to say even with no data at all.
    expect(telemetryTabItems(views()).map((t) => t.id)).toEqual(["turns"]);
  });

  it("adds a tab only when there are rows behind it", () => {
    expect(telemetryTabItems(views({ hasModels: true })).map((t) => t.id)).toEqual(["turns", "models"]);
    expect(telemetryTabItems(views({ hasTools: true })).map((t) => t.id)).toEqual(["turns", "tools"]);
    // A box that never called a tool has nothing to put under "By tool" — the same
    // reason Fleet is conditional, applied consistently.
    expect(telemetryTabItems(views({ hasModels: true, hasFleet: true })).map((t) => t.id)).not.toContain("tools");
  });

  it("adds Fleet only on a fleet install", () => {
    expect(telemetryTabItems(views({ hasFleet: true })).map((t) => t.id)).toContain("fleet");
    expect(telemetryTabItems(views()).map((t) => t.id)).not.toContain("fleet");
  });

  it("keeps a stable left-to-right order", () => {
    expect(telemetryTabItems(views({ hasModels: true, hasTools: true, hasFleet: true })).map((t) => t.id)).toEqual([
      "turns",
      "models",
      "tools",
      "fleet",
    ]);
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
    expect(hasTelemetryViews(false, false)).toBe(false);
  });

  it("is true when either side has something", () => {
    expect(hasTelemetryViews(true, false)).toBe(true);
    expect(hasTelemetryViews(false, true)).toBe(true);
  });
});
