import { describe, expect, it } from "vitest";

import { parseStringList } from "./SettingsCategory";

describe("parseStringList", () => {
  it("splits on commas", () => {
    expect(parseStringList("owner/a, owner/b")).toEqual(["owner/a", "owner/b"]);
  });
  it("splits on newlines too (back-compat with one-per-line)", () => {
    expect(parseStringList("owner/a\nowner/b")).toEqual(["owner/a", "owner/b"]);
  });
  it("mixes separators, trims, and drops empties", () => {
    expect(parseStringList("a , , b\n , c ")).toEqual(["a", "b", "c"]);
  });
  it("is empty for blank input", () => {
    expect(parseStringList("   ")).toEqual([]);
  });
});
