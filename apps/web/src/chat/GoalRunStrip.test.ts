import { describe, expect, it } from "vitest";

import type { GoalState } from "../lib/types";
import { goalStripState } from "./GoalRunStrip";

// The chat tab's goal strip: shows its OWN session's goal while it drives, then the outcome
// (green for achieved) for the recent window — unless dismissed.
const now = Date.UTC(2026, 9, 1, 12, 0, 0);
const g = (over: Partial<GoalState>): GoalState => ({
  session_id: "s1",
  condition: "tests pass",
  status: "active",
  ...over,
});

describe("goalStripState", () => {
  it("is driving while the session's goal is active", () => {
    expect(goalStripState([g({})], "s1", now, new Set())?.phase).toBe("driving");
  });

  it("ignores other sessions' goals", () => {
    expect(goalStripState([g({ session_id: "other" })], "s1", now, new Set())).toBeNull();
    expect(goalStripState(undefined, "s1", now, new Set())).toBeNull();
  });

  it("turns achieved / failed once the verifier decides", () => {
    const fin = now / 1000 - 60;
    expect(goalStripState([g({ status: "achieved", finished_at: fin })], "s1", now, new Set())?.phase).toBe("achieved");
    expect(goalStripState([g({ status: "exhausted", finished_at: fin })], "s1", now, new Set())?.phase).toBe("failed");
  });

  it("hides a finished goal once dismissed or past the recent window", () => {
    const fin = now / 1000 - 60;
    expect(goalStripState([g({ status: "achieved", finished_at: fin })], "s1", now, new Set([`s1:${fin}`]))).toBeNull();
    expect(goalStripState([g({ status: "achieved", finished_at: now / 1000 - 7200 })], "s1", now, new Set())).toBeNull();
  });
});
