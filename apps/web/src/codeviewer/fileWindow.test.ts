import { describe, expect, it } from "vitest";

import { countCutLines, LINE_CUT_MARKER, pageAfter, pageBefore, windowFor, WINDOW_SIZE } from "./fileWindow";

describe("windowFor", () => {
  it("a file that fits is fetched whole", () => {
    expect(windowFor(10, 500)).toEqual({ start: 1, end: 500 });
    expect(windowFor(19_000, WINDOW_SIZE)).toEqual({ start: 1, end: WINDOW_SIZE });
  });
  it("line 30,000 of 45,000 reaches EOF — the last line is never silently dropped", () => {
    expect(windowFor(30_000, 45_000)).toEqual({ start: 25_001, end: 45_000 });
  });
  it("keeps 5,000 lines of context above a target deep in a huge file", () => {
    expect(windowFor(30_000, 100_000)).toEqual({ start: 25_000, end: 44_999 });
  });
  it("ends at EOF for a target near the end, and never starts past it", () => {
    expect(windowFor(44_990, 45_000)).toEqual({ start: 25_001, end: 45_000 });
    expect(windowFor(99_999_999, 45_000)).toEqual({ start: 25_001, end: 45_000 });
  });
  it("the target is always inside the window", () => {
    for (const [line, lc] of [
      [1, 50_000],
      [25_000, 50_000],
      [34_000, 50_000],
      [50_000, 50_000],
      [20_001, 20_001],
    ]) {
      const w = windowFor(line, lc);
      expect(w.start).toBeLessThanOrEqual(line);
      expect(w.end).toBeGreaterThanOrEqual(line);
      expect(w.end - w.start + 1).toBeLessThanOrEqual(WINDOW_SIZE);
      expect(w.end).toBeLessThanOrEqual(lc);
    }
  });
});

describe("paging", () => {
  it("pages back to line 1 and forward to EOF, and stops at the edges", () => {
    expect(pageBefore({ start: 25_000, end: 44_999 })).toEqual({ start: 5_000, end: 24_999 });
    expect(pageBefore({ start: 3_000, end: 22_999 })).toEqual({ start: 1, end: 2_999 });
    expect(pageBefore({ start: 1, end: 20_000 })).toBeNull();
    expect(pageAfter({ start: 25_000, end: 44_999 }, 45_000)).toEqual({ start: 45_000, end: 45_000 });
    expect(pageAfter({ start: 25_001, end: 45_000 }, 45_000)).toBeNull();
  });
});

describe("countCutLines", () => {
  it("counts lines the server cut at the per-line cap", () => {
    expect(countCutLines(`a\n${"x".repeat(10)}${LINE_CUT_MARKER}\nb${LINE_CUT_MARKER}\n`)).toBe(2);
    expect(countCutLines("plain\ntext")).toBe(0);
    expect(countCutLines(null)).toBe(0);
  });
});
