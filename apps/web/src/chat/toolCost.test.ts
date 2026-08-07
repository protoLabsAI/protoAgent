import { describe, expect, it } from "vitest";

import { CHARS_PER_TOKEN, MIN_SHOWN_TOKENS, approxTokens, toolCostTokens } from "./toolCost";

describe("approxTokens", () => {
  it("floors chars/4, matching the backend's integer division", () => {
    expect(approxTokens("a".repeat(4000))).toBe(1000);
    // 4003/4 = 1000.75 → floor. The backend computes `chars // 4`; a rounding
    // mismatch here would show a different number than the prompt viewer's budget rows.
    expect(approxTokens("a".repeat(4003))).toBe(1000);
  });

  it("treats absent/empty output as zero rather than throwing", () => {
    expect(approxTokens(undefined)).toBe(0);
    expect(approxTokens(null)).toBe(0);
    expect(approxTokens("")).toBe(0);
  });

  it("uses the same divisor the module exports", () => {
    expect(approxTokens("a".repeat(CHARS_PER_TOKEN * 7))).toBe(7);
  });
});

describe("toolCostTokens", () => {
  const big = "x".repeat(MIN_SHOWN_TOKENS * CHARS_PER_TOKEN);

  it("shows the estimate for a settled call with a substantial result", () => {
    expect(toolCostTokens({ status: "done", output: big })).toBe(MIN_SHOWN_TOKENS);
  });

  it("stays silent below the threshold — a current_time card gets no chip", () => {
    expect(toolCostTokens({ status: "done", output: "2026-08-07T00:00:00Z" })).toBeNull();
  });

  it("is inclusive at the threshold", () => {
    expect(toolCostTokens({ status: "done", output: big })).not.toBeNull();
    expect(
      toolCostTokens({ status: "done", output: "x".repeat(MIN_SHOWN_TOKENS * CHARS_PER_TOKEN - CHARS_PER_TOKEN) }),
    ).toBeNull();
  });

  it("stays silent while the call is RUNNING — output is absent or partial mid-flight", () => {
    expect(toolCostTokens({ status: "running", output: undefined })).toBeNull();
    // The dangerous case: a partial output already long enough to cross the threshold
    // would otherwise render a number that is simply wrong, and climbs as chunks land.
    expect(toolCostTokens({ status: "running", output: big })).toBeNull();
  });

  it("stays silent on error — 'cost' misframes a failure", () => {
    expect(toolCostTokens({ status: "error", output: big })).toBeNull();
  });

  it("handles a settled call that returned nothing", () => {
    expect(toolCostTokens({ status: "done", output: undefined })).toBeNull();
  });
});
