import { describe, expect, it } from "vitest";

import { formatStringList, parseStringList } from "./SettingsCategory";

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
  // The empty-string SENTINEL (ADR 0069 D3a): knowledge.inject_namespaces uses a literal
  // "" entry to mean "the un-namespaced rows". Bare separators stay droppable noise; the
  // sentinel is spelled with quotes.
  it('keeps a quoted "" token as the empty-string entry', () => {
    expect(parseStringList('workspace, ""')).toEqual(["workspace", ""]);
    expect(parseStringList("''")).toEqual([""]);
  });
});

describe("formatStringList", () => {
  it("joins with commas", () => {
    expect(formatStringList(["a", "b"])).toBe("a, b");
  });
  it('spells the empty-string entry as "" (round-trips through parse)', () => {
    const items = ["workspace", ""];
    const text = formatStringList(items);
    expect(text).toBe('workspace, ""');
    expect(parseStringList(text)).toEqual(items);
  });
});
