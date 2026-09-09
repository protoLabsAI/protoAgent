import { describe, expect, it } from "vitest";

// #3412 — the console renders markdown tables through @protolabsai/ui, whose bundled
// @protolabsai/ui-css 0.60.2 fixes narrow table columns collapsing to one character per line
// (a stray inherited `overflow-wrap: anywhere` on markdown <table> cells). There is no
// console-side code or local CSS for the fix — it ships purely through the dependency range —
// so this guard is what fails loudly if the declared range is ever floored below 0.60.2 and
// silently reintroduces the unreadable-table regression. JSON module import (resolveJsonModule)
// rather than node:fs: this tsconfig has no node types, and under vitest+jsdom `import.meta.url`
// is an http: URL, so URL-relative filesystem access is a trap (see chatTabPalette.test.ts).
import pkg from "../package.json";

const deps = (pkg as { dependencies: Record<string, string> }).dependencies;
const UI_FIX_FLOOR: readonly [number, number, number] = [0, 60, 2];

// The lowest version a `^` / `~` / exact npm range can resolve to is its base version with the
// operator stripped — caret and tilde only widen the ceiling, they never lower the floor. This
// dependency uses one of those three forms, so the base version is the guaranteed minimum.
function minVersion(range: string): [number, number, number] {
  const base = range.replace(/^[\^~]/, "").trim();
  const parts = base.split(".");
  expect(parts).toHaveLength(3);
  const nums = parts.map((part) => Number.parseInt(part, 10));
  expect(nums.every((n) => Number.isInteger(n))).toBe(true);
  return [nums[0], nums[1], nums[2]];
}

function gte(a: readonly number[], b: readonly number[]): boolean {
  for (let i = 0; i < 3; i += 1) {
    if (a[i] !== b[i]) return a[i] > b[i];
  }
  return true;
}

describe("console @protolabsai/ui dependency floor (#3412)", () => {
  it("declares @protolabsai/ui as a runtime dependency", () => {
    expect(deps["@protolabsai/ui"]).toBeTruthy();
  });

  it("floors @protolabsai/ui at >= 0.60.2 so the markdown table-wrapping fix ships", () => {
    expect(gte(minVersion(deps["@protolabsai/ui"]), UI_FIX_FLOOR)).toBe(true);
  });

  it("keeps the caret range so compatible ui patches/minors are still accepted", () => {
    expect(deps["@protolabsai/ui"].startsWith("^")).toBe(true);
  });
});
