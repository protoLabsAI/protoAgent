// /perf (#2677) — chat-native performance snapshot. Mocks only api.telemetrySummary
// and api.telemetryInsights (importOriginal keeps the rest of the api real — this file
// registers every core command at import).

import { beforeEach, describe, expect, it, vi } from "vitest";

import type { SlashContext } from "../ext/slashRegistry";
import { findSlashCommand } from "../ext/slashRegistry";
import { api } from "../lib/api";
import "./coreSlashCommands";

vi.mock("../lib/api", async (importOriginal) => {
  const mod = await importOriginal<typeof import("../lib/api")>();
  return { ...mod, api: { ...mod.api, telemetrySummary: vi.fn(), telemetryInsights: vi.fn() } };
});

const telemetrySummary = vi.mocked(api.telemetrySummary);
const telemetryInsights = vi.mocked(api.telemetryInsights);

function ctx(over: Partial<SlashContext> = {}): SlashContext {
  return { rest: "", sessionId: "s1", noteToThread: () => {}, setDraft: () => {}, focusComposer: () => {}, ...over };
}

function call(over: Partial<SlashContext> = {}) {
  const noted: { md: string; tone?: string }[] = [];
  const handled = findSlashCommand("perf")!.run(
    ctx({ noteToThread: (md, opts) => noted.push({ md, tone: opts?.tone }), ...over }),
  );
  return { handled, noted };
}

const enabledSummary = {
  enabled: true,
  summary: {
    turns: 5,
    input_tokens: 0,
    output_tokens: 0,
    total_tokens: 0,
    cache_read_input_tokens: 0,
    cache_creation_input_tokens: 0,
    cost_usd: 1,
    llm_calls: 5,
    tool_calls: 2,
    avg_duration_ms: 1000,
    p50_duration_ms: 900,
    p95_duration_ms: 2000,
    p99_duration_ms: 3000,
    success_rate: 1,
    cache_hit_ratio: 0.5,
    by_model: [],
  },
};

beforeEach(() => {
  telemetrySummary.mockReset();
  telemetryInsights.mockReset();
});

describe("/perf", () => {
  it("falls through without a session", () => {
    expect(call({ sessionId: null }).handled).toBe(false);
    expect(telemetrySummary).not.toHaveBeenCalled();
  });

  it("says so when telemetry is off", async () => {
    telemetrySummary.mockResolvedValue({ enabled: false, summary: null });
    telemetryInsights.mockResolvedValue({ enabled: false, insights: null });
    const { noted } = call();
    await vi.waitFor(() => expect(noted.length).toBe(1));
    expect(noted[0].tone).toBe("warning");
    expect(noted[0].md).toContain("Telemetry is off");
  });

  it("renders the snapshot when both calls succeed", async () => {
    telemetrySummary.mockResolvedValue(enabledSummary);
    telemetryInsights.mockResolvedValue({ enabled: true, insights: { turns: 5, flagged: [], flagged_count: 0 } as never });
    const { noted } = call();
    await vi.waitFor(() => expect(noted.length).toBe(1));
    expect(noted[0].tone).toBe("info");
    expect(noted[0].md).toContain("Performance snapshot");
  });

  it("still renders the summary when the insights fetch fails (#2698 review fix)", async () => {
    // A rejected telemetryInsights() must not sink Promise.all and discard an
    // already-successful summary — perfNoteMarkdown supports null insights for
    // exactly this case.
    telemetrySummary.mockResolvedValue(enabledSummary);
    telemetryInsights.mockRejectedValue(new Error("insights boom"));
    const { noted } = call();
    await vi.waitFor(() => expect(noted.length).toBe(1));
    expect(noted[0].tone).toBe("info");
    expect(noted[0].md).toContain("Performance snapshot");
    expect(noted[0].md).not.toContain("insights boom");
  });

  it("surfaces a summary fetch failure as a danger note", async () => {
    telemetrySummary.mockRejectedValue(new Error("summary boom"));
    telemetryInsights.mockResolvedValue({ enabled: true, insights: { turns: 0, flagged: [], flagged_count: 0 } as never });
    const { noted } = call();
    await vi.waitFor(() => expect(noted.length).toBe(1));
    expect(noted[0].tone).toBe("danger");
    expect(noted[0].md).toContain("summary boom");
  });
});
