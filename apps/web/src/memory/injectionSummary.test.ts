import { describe, expect, it } from "vitest";

import { NOTHING_USED, injectionSummary } from "./injectionSummary";

// Build a counts-shaped row with arrays of the given lengths (contents are
// irrelevant — the summary reads only lengths).
function row(hot: number, digest: number, rag: number) {
  const fill = (n: number) => Array.from({ length: n }, (_, i) => i);
  return { hot_chunk_ids: fill(hot), digest_session_ids: fill(digest), rag_chunk_ids: fill(rag) };
}

describe("injectionSummary", () => {
  it("joins all three groups in memories → past chats → docs order", () => {
    expect(injectionSummary(row(3, 2, 4))).toBe("3 memories · 2 past chats · 4 docs");
  });

  it("uses singular labels for a single item in each group", () => {
    expect(injectionSummary(row(1, 1, 1))).toBe("1 memory · 1 past chat · 1 doc");
  });

  it("drops empty groups rather than showing a zero", () => {
    expect(injectionSummary(row(2, 0, 0))).toBe("2 memories");
    expect(injectionSummary(row(0, 0, 5))).toBe("5 docs");
    expect(injectionSummary(row(0, 1, 3))).toBe("1 past chat · 3 docs");
  });

  it("shows a dash when the turn injected nothing extra", () => {
    expect(injectionSummary(row(0, 0, 0))).toBe(NOTHING_USED);
  });
});
