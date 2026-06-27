import { describe, expect, it } from "vitest";

import { contextFromParts, costFromParts } from "./api";

const COST_MIME = "application/vnd.protolabs.cost-v1+json";
const CONTEXT_MIME = "application/vnd.protolabs.context-v1+json";

// costFromParts lifts the terminal cost-v1 DataPart (A2A ext) into the camelCase TurnUsage
// the per-turn footer renders. It must map snake_case wire fields, derive totalTokens, and
// match across every DataPart encoding the fleet emits (flattened `data` + 1.0 `content.value`).

describe("costFromParts", () => {
  const payload = {
    usage: {
      input_tokens: 12_340,
      output_tokens: 1_200,
      cache_read_input_tokens: 8_000,
      cache_creation_input_tokens: 320,
    },
    costUsd: 0.0412,
    durationMs: 2300,
    success: true,
  };

  it("maps the flattened-`data` encoding → camelCase usage, deriving totalTokens", () => {
    const usage = costFromParts([{ metadata: { mimeType: COST_MIME }, data: payload }]);
    expect(usage).toEqual({
      inputTokens: 12_340,
      outputTokens: 1_200,
      totalTokens: 13_540,
      cacheReadTokens: 8_000,
      cacheCreationTokens: 320,
      costUsd: 0.0412,
      durationMs: 2300,
    });
  });

  it("matches the A2A 1.0 member-discriminated encoding (content.$case === 'data')", () => {
    const usage = costFromParts([
      { metadata: { mimeType: COST_MIME }, content: { $case: "data", value: payload } },
    ]);
    expect(usage?.inputTokens).toBe(12_340);
    expect(usage?.totalTokens).toBe(13_540);
  });

  it("omits costUsd / durationMs when the wire payload omits them", () => {
    const usage = costFromParts([
      { metadata: { mimeType: COST_MIME }, data: { usage: { input_tokens: 10, output_tokens: 5 } } },
    ]);
    expect(usage).toEqual({
      inputTokens: 10,
      outputTokens: 5,
      totalTokens: 15,
      cacheReadTokens: 0,
      cacheCreationTokens: 0,
    });
    expect(usage).not.toHaveProperty("costUsd");
    expect(usage).not.toHaveProperty("durationMs");
  });

  it("returns null when there's no cost part, or the part lacks a usage block", () => {
    expect(costFromParts(undefined)).toBeNull();
    expect(costFromParts([{ metadata: { mimeType: "text/plain" }, data: { usage: {} } }])).toBeNull();
    expect(costFromParts([{ metadata: { mimeType: COST_MIME }, data: { costUsd: 1 } }])).toBeNull();
  });
});

describe("contextFromParts", () => {
  it("decodes the token-based context-v1 readout (with a compaction threshold)", () => {
    const ctx = contextFromParts([
      {
        metadata: { mimeType: CONTEXT_MIME },
        data: { contextTokens: 48_000, compactionAtTokens: 120_000, trigger: "tokens:120000", enabled: true },
      },
    ]);
    expect(ctx).toEqual({
      contextTokens: 48_000,
      compactionAtTokens: 120_000,
      trigger: "tokens:120000",
      enabled: true,
    });
  });

  it("keeps the size but omits the threshold for a non-token trigger (fraction/messages)", () => {
    const ctx = contextFromParts([
      { metadata: { mimeType: CONTEXT_MIME }, content: { $case: "data", value: { contextTokens: 9_000, trigger: "messages:80", enabled: true } } },
    ]);
    expect(ctx?.contextTokens).toBe(9_000);
    expect(ctx).not.toHaveProperty("compactionAtTokens");
    expect(ctx?.trigger).toBe("messages:80");
  });

  it("returns null with no context part or no contextTokens", () => {
    expect(contextFromParts(undefined)).toBeNull();
    expect(contextFromParts([{ metadata: { mimeType: CONTEXT_MIME }, data: { trigger: "tokens:1" } }])).toBeNull();
  });
});
