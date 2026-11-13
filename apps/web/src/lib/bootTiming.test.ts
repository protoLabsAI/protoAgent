import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { __resetBootTimingForTests, logBootTimings, markBoot } from "./bootTiming";

beforeEach(() => {
  performance.clearMarks?.();
  performance.clearMeasures?.();
  __resetBootTimingForTests();
});
afterEach(() => vi.restoreAllMocks());

describe("bootTiming", () => {
  it("marks each milestone once, even across re-renders", () => {
    markBoot("first-render");
    markBoot("first-render");
    expect(performance.getEntriesByName("pa:boot:first-render")).toHaveLength(1);
  });

  it("logs the three segment durations once, at debug level, when all marks exist", () => {
    const debug = vi.spyOn(console, "debug").mockImplementation(() => {});
    markBoot("script-start");
    markBoot("first-render");
    markBoot("ready");
    logBootTimings();
    logBootTimings(); // second call is a no-op
    expect(debug).toHaveBeenCalledTimes(1);
    const payload = debug.mock.calls[0][1] as Record<string, number>;
    expect(Object.keys(payload)).toEqual([
      "script-start→first-render",
      "first-render→ready",
      "script-start→ready",
    ]);
  });

  it("does nothing when a mark is missing", () => {
    const debug = vi.spyOn(console, "debug").mockImplementation(() => {});
    markBoot("script-start"); // no first-render / ready
    logBootTimings();
    expect(debug).not.toHaveBeenCalled();
  });
});
