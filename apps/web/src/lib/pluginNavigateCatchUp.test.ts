import { beforeEach, describe, expect, it } from "vitest";

import {
  NAVIGATE_CATCH_UP_TTL_MS,
  NAVIGATE_CATCH_UP_WINDOW_MS,
  armNavigateCatchUp,
  isPluginViewMounted,
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
  it("arms nothing for a view that is already mounted: open views keep their iframe and live relay", () => {
    const off = trackPluginViewMount(KEY, bus([], 0));
    expect(isPluginViewMounted(KEY)).toBe(true);
    expect(armNavigateCatchUp(KEY, bus([{ seq: 5, at: 0 }], 0))).toBe(false);
    expect(takeNavigateCatchUp(KEY, bus([], 0))).toBeNull();
    off();
    expect(isPluginViewMounted(KEY)).toBe(false);
  });

  it("counts overlapping mounts (StrictMode / key change), so one cleanup doesn't mark it closed", () => {
    const a = trackPluginViewMount(KEY, bus([], 0));
    const b = trackPluginViewMount(KEY, bus([], 0));
    a();
    expect(isPluginViewMounted(KEY)).toBe(true);
    b();
    expect(isPluginViewMounted(KEY)).toBe(false);
  });

  it("an unmounted view's catch-up starts after what its previous page was offered", () => {
    // The previous page saw through seq 7 (it unmounted then); the tool published seq 9.
    trackPluginViewMount(KEY, bus([{ seq: 7, at: 100 }], 100))();
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
    trackPluginViewMount(KEY, bus([{ seq: 2, at: 0 }], 0))();
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
