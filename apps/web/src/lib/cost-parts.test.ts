import { describe, expect, it } from "vitest";

import { contextFromParts, costFromMeta } from "./api";

const COST_EXT_URI = "https://proto-labs.ai/a2a/ext/cost-v1";
const WORLDSTATE_EXT_URI = "https://proto-labs.ai/a2a/ext/worldstate-delta-v1";
const CONFIDENCE_EXT_URI = "https://proto-labs.ai/a2a/ext/confidence-v1";
const CONTEXT_MIME = "application/vnd.protolabs.context-v1+json";

// costFromMeta lifts the terminal cost-v1 extension into the camelCase TurnUsage the
// per-turn footer renders. Since protolabs-a2a 0.3.0 the payload rides the ARTIFACT'S
// METADATA map keyed by the extension URI (not a MIME-typed DataPart), so this reads a
// metadata map; it must still map the snake_case wire fields and derive totalTokens.

describe("costFromMeta", () => {
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

  it("maps the URI-keyed metadata → camelCase usage, deriving totalTokens", () => {
    const usage = costFromMeta({ [COST_EXT_URI]: payload });
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

  it("picks its own fragment out of a metadata map carrying several extensions", () => {
    // The terminal artifact merges cost + worldstate into ONE metadata map
    // (merge_extension_metadata), so the reader must select strictly by URI.
    const usage = costFromMeta({ [WORLDSTATE_EXT_URI]: { deltas: [] }, [COST_EXT_URI]: payload });
    expect(usage?.inputTokens).toBe(12_340);
    expect(usage?.totalTokens).toBe(13_540);
  });

  it("omits costUsd / durationMs when the wire payload omits them", () => {
    const usage = costFromMeta({ [COST_EXT_URI]: { usage: { input_tokens: 10, output_tokens: 5 } } });
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

  it("returns null with no metadata, a different extension, or no usage block", () => {
    expect(costFromMeta(undefined)).toBeNull();
    expect(costFromMeta({ [CONFIDENCE_EXT_URI]: { confidence: 0.9 } })).toBeNull();
    expect(costFromMeta({ [COST_EXT_URI]: { costUsd: 1 } })).toBeNull();
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
