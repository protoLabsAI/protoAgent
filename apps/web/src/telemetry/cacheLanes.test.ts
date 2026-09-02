import { describe, expect, it } from "vitest";

import { coldLaneLabel, laneCacheHit } from "./cacheLanes";
import type { TelemetryByModelRow } from "../lib/types";

const pct = (n: number) => `${Math.round(n * 100)}%`;

const lane = (over: Partial<TelemetryByModelRow> = {}): TelemetryByModelRow => ({
  model: "openai-codex",
  turns: 38,
  cost_usd: 1,
  total_tokens: 1000,
  p50_duration_ms: 0,
  p95_duration_ms: 0,
  p99_duration_ms: 0,
  ...over,
});

describe("coldLaneLabel", () => {
  it("names the cold lane, not the dominant one", () => {
    // The whole point of #3342: the dominant model is claude-opus-5, which caches
    // beautifully. Naming it would send an operator to fix the healthy lane.
    expect(
      coldLaneLabel({
        hit_ratio: 0.51,
        read_tokens: 4_000_000,
        est_savings_usd: 12,
        engaging: false,
        model: "claude-opus-5",
        cold_lanes: [{ model: "openai-codex", turns: 38 }],
      }),
    ).toBe("openai-codex");
  });

  it("lists every cold lane", () => {
    expect(
      coldLaneLabel({
        hit_ratio: 0,
        read_tokens: 0,
        est_savings_usd: 0,
        engaging: false,
        model: "a",
        cold_lanes: [{ model: "a", turns: 10 }, { model: "b", turns: 11 }],
      }),
    ).toBe("a, b");
  });

  it("falls back to the dominant model for a backend that sends no lanes", () => {
    // A fleet member on a pre-#3342 release. Better a slightly wrong name than none.
    expect(
      coldLaneLabel({ hit_ratio: 0, read_tokens: 0, est_savings_usd: 0, engaging: false, model: "gpt-5.6" }),
    ).toBe("gpt-5.6");
  });

  it("says something rather than nothing when even that is missing", () => {
    expect(coldLaneLabel({ hit_ratio: 0, read_tokens: 0, est_savings_usd: 0, engaging: false })).toBe(
      "this agent",
    );
  });
});

describe("laneCacheHit", () => {
  it("shows the lane's own ratio", () => {
    expect(laneCacheHit(lane({ cache_hit_ratio: 0.9, input_tokens: 1000, cache_read_input_tokens: 9000 }), pct)).toBe(
      "90%",
    );
  });

  it("shows 0% for a lane that measured prompts and cached none of them", () => {
    // This one IS a defect and must read as one — it is the case #3342 is about.
    expect(laneCacheHit(lane({ cache_hit_ratio: 0, input_tokens: 10_000 }), pct)).toBe("0%");
  });

  it("shows nothing for a lane that recorded no prompt tokens at all", () => {
    // An ACP coder leg runs outside the gateway and reports no usage (#3015).
    // 0% there accuses a lane that was never measured.
    expect(laneCacheHit(lane({ model: "acp:coder", cache_hit_ratio: 0, input_tokens: 0 }), pct)).toBe("—");
  });

  it("shows nothing for a pre-#3342 backend that sends no ratio", () => {
    expect(laneCacheHit(lane(), pct)).toBe("—");
  });
});
