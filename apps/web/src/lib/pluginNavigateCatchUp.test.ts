import { beforeEach, describe, expect, it } from "vitest";

import {
  NAVIGATE_CATCH_UP_TTL_MS,
  NAVIGATE_CATCH_UP_WINDOW_MS,
  armNavigateCatchUp,
  isPluginViewLive,
  resetNavigateCatchUp,
  takeNavigateCatchUp,
  trackPluginViewMount,
} from "./pluginNavigateCatchUp";

const KEY = "plugin:atlas:atlas";

// A fake bus: `seqReceivedBefore(cutoff)` answers from frames that arrived at known times.
function bus(frames: Array<{ seq: number; at: number }>, now: number) {
  return {
    now: () => now,
    latestSeq: () => (frames.length ? frames[frames.length - 1].seq : null),
    seqReceivedBefore: (cutoff: number) => {
      const before = frames.filter((f) => f.at <= cutoff);
      return before.length ? before[before.length - 1].seq : null;
    },
  };
}

beforeEach(() => resetNavigateCatchUp());

describe("navigate catch-up (#4283)", () => {
  it("arms nothing for a view whose page is live (subscribed): it keeps its iframe and relay", () => {
    const t = trackPluginViewMount(KEY, bus([], 0));
    t.markSubscribed();
    expect(isPluginViewLive(KEY)).toBe(true);
    expect(armNavigateCatchUp(KEY, bus([{ seq: 5, at: 0 }], 0))).toBe(false);
    expect(takeNavigateCatchUp(KEY, bus([], 0))).toBeNull();
    t.release();
    expect(isPluginViewLive(KEY)).toBe(false);
  });

  it("a mounted page that hasn't subscribed yet still gets the catch-up (nothing reached it live)", () => {
    trackPluginViewMount(KEY, bus([], 0)); // probing / loading, no subscribe yet
    expect(isPluginViewLive(KEY)).toBe(false);
    expect(armNavigateCatchUp(KEY, bus([{ seq: 5, at: 0 }], 0))).toBe(true);
    expect(takeNavigateCatchUp(KEY, bus([], 0))).toBe(-1);
  });

  it("a page that unmounts before subscribing leaves the high-water mark alone", () => {
    const seen = trackPluginViewMount(KEY, bus([{ seq: 3, at: 0 }], 0));
    seen.markSubscribed();
    seen.release(); // offered through seq 3
    trackPluginViewMount(KEY, bus([{ seq: 3, at: 0 }, { seq: 8, at: 1 }], 1)).release(); // never subscribed
    armNavigateCatchUp(KEY, bus([{ seq: 3, at: 0 }, { seq: 8, at: 1 }], 2));
    expect(takeNavigateCatchUp(KEY, bus([], 2))).toBe(3); // seq 8 still replays
  });

  it("counts overlapping live pages (StrictMode / key change), so one release doesn't mark it closed", () => {
    const a = trackPluginViewMount(KEY, bus([], 0));
    const b = trackPluginViewMount(KEY, bus([], 0));
    a.markSubscribed();
    b.markSubscribed();
    a.release();
    a.release(); // idempotent
    expect(isPluginViewLive(KEY)).toBe(true);
    b.release();
    expect(isPluginViewLive(KEY)).toBe(false);
  });

  it("an unmounted view's catch-up starts after what its previous page was offered", () => {
    // The previous page saw through seq 7 (it unmounted then); the tool published seq 9.
    const prev = trackPluginViewMount(KEY, bus([{ seq: 7, at: 100 }], 100));
    prev.markSubscribed();
    prev.release();
    const now = 200;
    expect(armNavigateCatchUp(KEY, bus([{ seq: 7, at: 100 }, { seq: 9, at: 199 }], now))).toBe(true);
    expect(takeNavigateCatchUp(KEY, bus([], now))).toBe(7);
    // One-shot: a re-subscribe on the same page gets no second replay.
    expect(takeNavigateCatchUp(KEY, bus([], now))).toBeNull();
  });

  it("a view never mounted this session only catches up on the window before the navigate", () => {
    const now = 100_000;
    const old = now - NAVIGATE_CATCH_UP_WINDOW_MS - 1;
    armNavigateCatchUp(KEY, bus([{ seq: 3, at: old }, { seq: 4, at: now - 5 }], now));
    expect(takeNavigateCatchUp(KEY, bus([], now))).toBe(3); // replaySince(3) → seq 4 only
  });

  it("with nothing older than the window retained, the catch-up covers everything retained", () => {
    armNavigateCatchUp(KEY, bus([{ seq: 4, at: 99 }], 100));
    expect(takeNavigateCatchUp(KEY, bus([], 100))).toBe(-1);
  });

  it("the window wins over an OLDER high-water mark (a view closed long ago gets no backlog)", () => {
    const prev = trackPluginViewMount(KEY, bus([{ seq: 2, at: 0 }], 0));
    prev.markSubscribed();
    prev.release();
    const now = 100_000;
    armNavigateCatchUp(KEY, bus([{ seq: 2, at: 0 }, { seq: 50, at: 1_000 }, { seq: 51, at: now - 1 }], now));
    expect(takeNavigateCatchUp(KEY, bus([], now))).toBe(50);
  });

  it("expires if the page never subscribes in time", () => {
    armNavigateCatchUp(KEY, bus([], 0));
    expect(takeNavigateCatchUp(KEY, bus([], NAVIGATE_CATCH_UP_TTL_MS + 1))).toBeNull();
  });

  it("is per view: arming one view leaves the others alone", () => {
    armNavigateCatchUp(KEY, bus([], 0));
    expect(takeNavigateCatchUp("plugin:atlas:other", bus([], 0))).toBeNull();
    expect(takeNavigateCatchUp(KEY, bus([], 0))).toBe(-1);
  });
});
