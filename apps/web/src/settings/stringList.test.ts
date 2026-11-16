// @vitest-environment node
import { describe, expect, it } from "vitest";

import { formatStringList, moveListItem, parseStringList, settingListItems } from "./SettingsCategory";

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

// ADR 0133: an options-backed string_list role field (model.name, routing.aux_model, …) may
// arrive as a legacy SCALAR. The editor must show the configured model, not a blank row.
describe("settingListItems", () => {
  it("coerces a non-empty scalar string to a one-item chain", () => {
    expect(settingListItems("protolabs/reasoning")).toEqual(["protolabs/reasoning"]);
  });
  it("treats a blank or whitespace scalar as an empty chain", () => {
    expect(settingListItems("")).toEqual([]);
    expect(settingListItems("   ")).toEqual([]);
  });
  it("passes a list through, keeping only its string entries in order", () => {
    expect(settingListItems(["a", "b"])).toEqual(["a", "b"]);
    expect(settingListItems(["a", 1, null, "b"] as unknown)).toEqual(["a", "b"]);
  });
  it("is empty for null / undefined / non-string scalars", () => {
    expect(settingListItems(null)).toEqual([]);
    expect(settingListItems(undefined)).toEqual([]);
    expect(settingListItems(42)).toEqual([]);
  });
});

// The ordered-list up/down buttons (#1957 — favorite models, fallback models).
describe("moveListItem", () => {
  it("swaps an item with its neighbor, without mutating the input", () => {
    const items = ["a", "b", "c"];
    expect(moveListItem(items, 0, 1)).toEqual(["b", "a", "c"]);
    expect(moveListItem(items, 2, -1)).toEqual(["a", "c", "b"]);
    expect(items).toEqual(["a", "b", "c"]);
  });
  it("no-ops at the boundaries and on out-of-range indices", () => {
    const items = ["a", "b"];
    expect(moveListItem(items, 0, -1)).toBe(items);
    expect(moveListItem(items, 1, 1)).toBe(items);
    expect(moveListItem(items, 5, -1)).toBe(items);
  });
});
