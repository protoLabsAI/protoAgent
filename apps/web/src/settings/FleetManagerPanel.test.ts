import { describe, it, expect } from "vitest";

import { BOX_RUNTIME_KEYS, canAddRemote, slugOf } from "./FleetManagerPanel";

describe("canAddRemote — manual add-remote submit gate (ADR 0042 §I)", () => {
  it("requires a non-empty name", () => {
    expect(canAddRemote("", "http://100.64.0.9:7870")).toBe(false);
    expect(canAddRemote("   ", "http://100.64.0.9:7870")).toBe(false);
    expect(canAddRemote("ava", "http://100.64.0.9:7870")).toBe(true);
  });

  it("requires an http(s) URL", () => {
    expect(canAddRemote("ava", "")).toBe(false);
    expect(canAddRemote("ava", "100.64.0.9:7870")).toBe(false); // no scheme
    expect(canAddRemote("ava", "ftp://host")).toBe(false);
    expect(canAddRemote("ava", "ws://host")).toBe(false); // ws:// isn't the register URL (it's http)
    expect(canAddRemote("ava", "https://ava.example:7870")).toBe(true);
  });

  it("trims before validating", () => {
    expect(canAddRemote("  ava  ", "  http://host:7870  ")).toBe(true);
  });
});

describe("slugOf — the row link's destination (#2240)", () => {
  it("maps this instance to the reserved host slug", () => {
    expect(slugOf({ id: "abc123", host: true })).toBe("host");
  });

  it("uses the stable id for every other member", () => {
    expect(slugOf({ id: "abc123" })).toBe("abc123");
    expect(slugOf({ id: "abc123", host: false })).toBe("abc123");
  });

  it("never uses the display name — a rename must not move the agent's URL", () => {
    // The panel renames in place (id survives); the link has to keep pointing at the id.
    expect(slugOf({ id: "abc123" } as { id: string; name?: string })).toBe("abc123");
  });
});

describe("BOX_RUNTIME_KEYS — the Box runtime chip's field set (#2880)", () => {
  it("includes fleet.autostart alongside the keep-warm fields", () => {
    expect(BOX_RUNTIME_KEYS).toContain("fleet.autostart");
    expect(BOX_RUNTIME_KEYS).toContain("fleet.warm.max");
    expect(BOX_RUNTIME_KEYS).toContain("fleet.warm.grace_seconds");
  });

  it("pins the full set — a dropped key would silently vanish from the popover", () => {
    // QuickSetting resolves keys against the schema and silently skips misses, so a
    // typo or accidental removal here shows up nowhere else. Golden-pin the list.
    expect(BOX_RUNTIME_KEYS).toEqual([
      "network.bind",
      "fleet.port_base",
      "fleet.discovery.port_min",
      "fleet.discovery.port_max",
      "fleet.discovery.mdns",
      "fleet.warm.max",
      "fleet.warm.grace_seconds",
      "fleet.autostart",
    ]);
  });
});
