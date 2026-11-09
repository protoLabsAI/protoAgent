import { describe, expect, it } from "vitest";

import { evictionToast } from "./StorageUpkeep";

describe("the freed-storage toast says what happened, per agent", () => {
  const report = (slugs: string[]) => ({
    reason: "budget" as const,
    freedBytes: 1,
    evicted: slugs.map((slug, i) => ({ key: `k${i}`, slug, bytes: 1 })),
  });

  it("counts agents, not keys", () => {
    // A chat blob and a DM thread of the same agent are one agent's history.
    expect(evictionToast(report(["a", "a"])).message).toBe(
      "Cleared the saved chat history of an agent you haven't opened lately, so new chats keep saving.",
    );
    expect(evictionToast(report(["a", "b", "b"])).message).toBe(
      "Cleared the saved chat history of 2 agents you haven't opened lately, so new chats keep saving.",
    );
    expect(evictionToast(report(["a"])).title).toBe("Freed up browser storage");
  });
});
