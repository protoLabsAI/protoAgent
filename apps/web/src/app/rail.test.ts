import { describe, it, expect } from "vitest";

import { dedupeRailById } from "./rail";

describe("dedupeRailById (#1755 rail hardening)", () => {
  it("keeps the first occurrence of each id and drops later duplicates", () => {
    // A fork-contributed surface whose id collides with a core surface already in the list: the
    // core one wins (it comes first), the fork dup is dropped — no duplicate rail button/key.
    const items = [
      { id: "chat", label: "Chat" },
      { id: "plugin:github:prs", label: "Pull Requests" },
      { id: "chat", label: "Chat (fork override)" },
    ];
    const out = dedupeRailById(items);
    expect(out.map((i) => i.id)).toEqual(["chat", "plugin:github:prs"]);
    expect(out[0].label).toBe("Chat"); // first wins — the fork dup does not replace it
  });

  it("is a no-op (preserving order) when every id is unique", () => {
    const items = [{ id: "a" }, { id: "b" }, { id: "c" }];
    expect(dedupeRailById(items)).toEqual(items);
  });

  it("collapses several duplicates of the same id to one", () => {
    const items = [{ id: "x" }, { id: "x" }, { id: "y" }, { id: "x" }];
    expect(dedupeRailById(items).map((i) => i.id)).toEqual(["x", "y"]);
  });

  it("handles an empty list", () => {
    expect(dedupeRailById([])).toEqual([]);
  });
});
