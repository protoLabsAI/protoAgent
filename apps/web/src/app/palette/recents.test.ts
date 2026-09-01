import { beforeEach, describe, expect, it } from "vitest";

import { readAgentRecency, markAgentOpened } from "../fleetPalette";
import {
  agentKey,
  commandKey,
  frecency,
  markAgentUsed,
  markCommandUsed,
  markPaletteUsed,
  migrateFleetRecency,
  readPaletteRecency,
} from "./recents";

const KEY = "protoagent.palette.recent";
const LEGACY = "protoagent.fleet.recent";
const DAY = 24 * 60 * 60 * 1000;

beforeEach(() => localStorage.clear());

describe("namespacing (the reason this is a new key)", () => {
  it("keeps a command id and an agent slug that collide apart", () => {
    // A plugin shipping a command with id "ava" must not clobber agent ava's recency —
    // which is exactly what the old flat slug->timestamp map would have done.
    markCommandUsed("ava", 1_000);
    markAgentUsed("ava", 2_000);
    const map = readPaletteRecency();
    expect(map[commandKey("ava")]).toEqual({ n: 1, t: 1_000 });
    expect(map[agentKey("ava")]).toEqual({ n: 1, t: 2_000 });
  });

  it("counts uses as well as recency, so a habit outlives one-off curiosity", () => {
    markCommandUsed("settings", 1_000);
    markCommandUsed("settings", 2_000);
    expect(readPaletteRecency()[commandKey("settings")]).toEqual({ n: 2, t: 2_000 });
  });
});

describe("frecency", () => {
  const now = 10 * DAY;
  it("is zero for something never used", () => {
    expect(frecency(undefined, now)).toBe(0);
  });

  it("decays with age, so yesterday's single use beats last month's single use", () => {
    const yesterday = frecency({ n: 1, t: now - DAY }, now);
    const longAgo = frecency({ n: 1, t: now - 30 * DAY }, now);
    expect(yesterday).toBeGreaterThan(longAgo);
  });

  it("counts uses, so a repeatedly-run command outranks an equally recent one-off", () => {
    expect(frecency({ n: 5, t: now }, now)).toBeGreaterThan(frecency({ n: 1, t: now }, now));
  });
});

describe("migration off protoagent.fleet.recent", () => {
  it("seeds agent:<slug> entries from the legacy flat map on the first read", () => {
    localStorage.setItem(LEGACY, JSON.stringify({ ava: 100, bob: 200 }));
    expect(readPaletteRecency()).toEqual({
      "agent:ava": { n: 1, t: 100 },
      "agent:bob": { n: 1, t: 200 },
    });
  });

  it("runs ONCE — the new key's existence is the marker, so later legacy writes don't re-seed", () => {
    localStorage.setItem(LEGACY, JSON.stringify({ ava: 100 }));
    readPaletteRecency(); // migrates
    markAgentOpened("bob", 999); // a later write to the LEGACY store
    expect(readPaletteRecency()["agent:bob"]).toBeUndefined();
  });

  it("leaves the legacy key byte-for-byte alone (fleetPalette.test.ts asserts it EXACTLY)", () => {
    markAgentOpened("ava", 100);
    markAgentOpened("bob", 200);
    readPaletteRecency();
    markCommandUsed("settings");
    // Any wrapper or sub-map written under the old key would red fleetPalette.test.ts:11-16.
    expect(readAgentRecency()).toEqual({ ava: 100, bob: 200 });
    expect(JSON.parse(localStorage.getItem(LEGACY)!)).toEqual({ ava: 100, bob: 200 });
  });

  it("survives a corrupt or absent legacy value", () => {
    localStorage.setItem(LEGACY, "{not json");
    expect(migrateFleetRecency()).toEqual({});
    expect(readPaletteRecency()).toEqual({});
  });
});

describe("durability", () => {
  it("ignores malformed entries rather than rendering junk rows", () => {
    localStorage.setItem(
      KEY,
      JSON.stringify({ "cmd:ok": { n: 2, t: 5 }, "cmd:bad": { n: 1 }, "cmd:worse": 7 }),
    );
    expect(readPaletteRecency()).toEqual({ "cmd:ok": { n: 2, t: 5 } });
  });

  it("bounds the store instead of growing forever", () => {
    for (let i = 0; i < 200; i += 1) markPaletteUsed(`cmd:c${i}`, 1_000 + i);
    const size = Object.keys(readPaletteRecency()).length;
    expect(size).toBeLessThanOrEqual(120);
    // The most recent survivors are the ones kept.
    expect(readPaletteRecency()["cmd:c199"]).toBeDefined();
  });

  it("never throws when localStorage is unavailable", () => {
    const original = Object.getOwnPropertyDescriptor(globalThis, "localStorage")!;
    Object.defineProperty(globalThis, "localStorage", {
      configurable: true,
      get() {
        throw new Error("denied");
      },
    });
    try {
      expect(readPaletteRecency()).toEqual({});
      expect(() => markCommandUsed("settings")).not.toThrow();
    } finally {
      Object.defineProperty(globalThis, "localStorage", original);
    }
  });
});
