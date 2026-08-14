import { describe, expect, it } from "vitest";

import type { TelemetryInsights, TelemetrySummary } from "../lib/types";
import { byModelLine, byToolLine, perfNoteMarkdown } from "./perfView";

function mkSummary(over: Partial<TelemetrySummary> = {}): TelemetrySummary {
  return {
    turns: 42,
    input_tokens: 100000,
    output_tokens: 20000,
    total_tokens: 120000,
    cache_read_input_tokens: 60000,
    cache_creation_input_tokens: 5000,
    cost_usd: 12.34,
    llm_calls: 80,
    tool_calls: 30,
    avg_duration_ms: 2500,
    p50_duration_ms: 1200,
    p95_duration_ms: 4800,
    success_rate: 0.98,
    cache_hit_ratio: 0.62,
    p99_duration_ms: 9500,
    by_model: [
      {
        model: "protolabs/reasoning",
        turns: 30,
        cost_usd: 9.0,
        total_tokens: 90000,
        p50_duration_ms: 1000,
        p95_duration_ms: 4000,
        p99_duration_ms: 8000,
      },
      {
        model: "gpt-5.6-sol",
        turns: 12,
        cost_usd: 3.34,
        total_tokens: 30000,
        p50_duration_ms: 900,
        p95_duration_ms: 3500,
        p99_duration_ms: 7000,
      },
    ],
    by_tool: [
      { tool: "web_search", calls: 18, p50_duration_ms: 800, p95_duration_ms: 2200, p99_duration_ms: 3000 },
      { tool: "calculator", calls: 12, p50_duration_ms: 20, p95_duration_ms: 40, p99_duration_ms: 60 },
    ],
    ...over,
  };
}

function mkInsights(over: Partial<TelemetryInsights> = {}): TelemetryInsights {
  return {
    turns: 42,
    flagged: [],
    flagged_count: 0,
    levers: {
      cache: { hit_ratio: 0.62, read_tokens: 60000, est_savings_usd: 1.2 },
      routing: { by_model: [] },
      success_rate: 0.98,
    },
    unproven_levers: [],
    ...over,
  };
}

describe("byModelLine", () => {
  it("sorts by turns descending and formats model/turns/cost", () => {
    const summary = mkSummary();
    expect(byModelLine(summary)).toBe("protolabs/reasoning (30 turns, $9.00) · gpt-5.6-sol (12 turns, $3.34)");
  });

  it("caps at topN", () => {
    const summary = mkSummary({
      by_model: ["a", "b", "c", "d"].map((model, i) => ({
        model,
        turns: 5 - i,
        cost_usd: 1,
        total_tokens: 100,
        p50_duration_ms: 100,
        p95_duration_ms: 200,
        p99_duration_ms: 300,
      })),
    });
    expect(byModelLine(summary, 2)).toBe("a (5 turns, $1.00) · b (4 turns, $1.00)");
  });

  it("is empty when there's no by_model data", () => {
    expect(byModelLine(mkSummary({ by_model: [] }))).toBe("");
  });
});

describe("byToolLine", () => {
  it("formats tool/p95/calls, already server-sorted by p95 descending", () => {
    const summary = mkSummary();
    expect(byToolLine(summary)).toBe("web_search (2.2s p95, 18 calls) · calculator (40ms p95, 12 calls)");
  });

  it("caps at topN without re-sorting (server order is trusted)", () => {
    const summary = mkSummary({
      by_tool: ["a", "b", "c", "d"].map((tool, i) => ({
        tool,
        calls: 5 - i,
        p50_duration_ms: 100,
        p95_duration_ms: 400 - i * 100,
        p99_duration_ms: 500,
      })),
    });
    expect(byToolLine(summary, 2)).toBe("a (400ms p95, 5 calls) · b (300ms p95, 4 calls)");
  });

  it("is empty when there's no by_tool data (an instance with no recorded durations yet)", () => {
    expect(byToolLine(mkSummary({ by_tool: [] }))).toBe("");
  });
});

describe("perfNoteMarkdown", () => {
  it("renders a no-data note when there are zero turns", () => {
    expect(perfNoteMarkdown(mkSummary({ turns: 0 }), null)).toBe(
      "**Performance snapshot**\n\nNo turns recorded yet.",
    );
  });

  it("renders a no-data note when summary is null", () => {
    expect(perfNoteMarkdown(null, null)).toBe("**Performance snapshot**\n\nNo turns recorded yet.");
  });

  it("renders the header, stats, and by-model line", () => {
    const md = perfNoteMarkdown(mkSummary(), mkInsights());
    expect(md).toContain("**Performance snapshot** — last 42 turns");
    expect(md).toContain("p50 1.2s");
    expect(md).toContain("p95 4.8s");
    expect(md).toContain("success 98%");
    expect(md).toContain("cache-hit 62%");
    expect(md).toContain("cost $12.34");
    expect(md).toContain("_By model:_ protolabs/reasoning (30 turns, $9.00)");
    expect(md).toContain("_Slowest tools:_ web_search (2.2s p95, 18 calls)");
  });

  it("omits the tools line when there's no by_tool data", () => {
    const md = perfNoteMarkdown(mkSummary({ by_tool: [] }), mkInsights());
    expect(md).not.toContain("Slowest tools");
  });

  it("omits the outlier line when nothing is flagged", () => {
    const md = perfNoteMarkdown(mkSummary(), mkInsights({ flagged: [] }));
    expect(md).not.toContain("outlier");
  });

  it("surfaces flagged outlier turns when insights has them", () => {
    const flagged = [
      {
        task_id: "t1",
        session_id: "s1",
        state: "completed",
        success: 1,
        model: "x",
        models: "x",
        input_tokens: 0,
        output_tokens: 0,
        total_tokens: 0,
        cache_read_input_tokens: 0,
        cache_creation_input_tokens: 0,
        cost_usd: 5,
        duration_ms: 60000,
        llm_calls: 1,
        tool_calls: 0,
        created_at: "",
        ended_at: "",
        reasons: ["duration 5x median"],
      },
    ] as TelemetryInsights["flagged"];
    const md = perfNoteMarkdown(mkSummary(), mkInsights({ flagged }));
    expect(md).toContain("⚠️ 1 outlier turn flagged");
  });

  it("still renders a useful summary when insights is null (fetch skipped/failed)", () => {
    const md = perfNoteMarkdown(mkSummary(), null);
    expect(md).toContain("**Performance snapshot**");
    expect(md).not.toContain("outlier");
  });
});
